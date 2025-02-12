import os
from typing import Any, Callable, Dict, Optional, Type
import logging
from abc import ABC, abstractmethod

from guardrails.validator_base import (
    FailResult,
    PassResult,
    ValidationResult,
    Validator,
    register_validator,
)
from guardrails.stores.context import get_call_kwarg
from litellm import completion, get_llm_provider

logger = logging.getLogger(__name__)


class ArizeRagEvalPromptBase(ABC):
    def __init__(self, prompt_name, **kwargs) -> None:
        self.prompt_name = prompt_name

    @abstractmethod
    def generate_prompt(self, user_input_message: str, reference_text: str, llm_response: str) -> str:
        pass


class ContextRelevancyPrompt(ArizeRagEvalPromptBase):
    def generate_prompt(self, user_input_message: str, reference_text: str, llm_response: str) -> str:
        return f"""
            You are comparing a reference text to a question and trying to determine if the reference text
            contains information relevant to answering the question. Here is the data:
                [BEGIN DATA]
                ************
                [Question]: {user_input_message}
                ************
                [Reference text]: {reference_text}
                ************
                [END DATA]
            Compare the Question above to the Reference text. You must determine whether the Reference text
            contains information that can answer the Question. Please focus on whether the very specific
            question can be answered by the information in the Reference text.
            Your response must be single word, either "relevant" or "unrelated",
            and should not contain any text or characters aside from that word.
            "unrelated" means that the reference text does not contain an answer to the Question.
            "relevant" means the reference text contains an answer to the Question."""
    

class HallucinationPrompt(ArizeRagEvalPromptBase):
    def generate_prompt(self, user_input_message: str, reference_text: str, llm_response: str) -> str:
        return f"""
            In this task, you will be presented with a query, a reference text and an answer. The answer is
            generated to the question based on the reference text. The answer may contain false information. You
            must use the reference text to determine if the answer to the question contains false information,
            if the answer is a hallucination of facts. Your objective is to determine whether the answer text
            contains factual information and is not a hallucination. A 'hallucination' refers to
            an answer that is not based on the reference text or assumes information that is not available in
            the reference text. Your response should be a single word: either "factual" or "hallucinated", and
            it should not include any other text or characters. "hallucinated" indicates that the answer
            provides factually inaccurate information to the query based on the reference text. "factual"
            indicates that the answer to the question is correct relative to the reference text, and does not
            contain made up information. Please read the query and reference text carefully before determining
            your response.

                [BEGIN DATA]
                ************
                [Query]: {user_input_message}
                ************
                [Reference text]: {reference_text}
                ************
                [Answer]: {llm_response}
                ************
                [END DATA]

                Is the answer above factual or hallucinated based on the query and reference text?
            """
    

class QACorrectnessPrompt(ArizeRagEvalPromptBase):
    def generate_prompt(self, user_input_message: str, reference_text: str, llm_response: str) -> str:
        return f"""
            You are given a question, an answer and reference text. You must determine whether the
            given answer correctly answers the question based on the reference text. Here is the data:
                [BEGIN DATA]
                ************
                [Question]: {user_input_message}
                ************
                [Reference]: {reference_text}
                ************
                [Answer]: {llm_response}
                [END DATA]
            Your response must be a single word, either "correct" or "incorrect",
            and should not contain any text or characters aside from that word.
            "correct" means that the question is correctly and fully answered by the answer.
            "incorrect" means that the question is not correctly or only partially answered by the
            answer.
            """


@register_validator(name="arize/llm_rag_evaluator", data_type="string")
class LlmRagEvaluator(Validator):
    """This class validates an output generated by a LiteLLM (LLM) model by prompting another LLM model to evaluate the output.

    **Key Properties**

    | Property                      | Description                       |
    | ----------------------------- | --------------------------------- |
    | Name for `format` attribute   | `arize/relevancy_evaluator`       |
    | Supported data types          | `string`                          |
    | Programmatic fix              | N/A                               |

    Args:
        llm_callable (str, optional): The name of the LiteLLM model to use for validation. Defaults to "gpt-3.5-turbo".
        on_fail (Callable, optional): A function to be called when validation fails. Defaults to None.
    """

    def __init__(
        self,
        eval_llm_prompt_generator: Type[ArizeRagEvalPromptBase],
        llm_evaluator_fail_response: str,
        llm_evaluator_pass_response: str, 
        llm_callable: str,
        on_fail: Optional[Callable] = "noop",
        **kwargs,
    ):
        super().__init__(
            on_fail,
            eval_llm_prompt_generator=eval_llm_prompt_generator,
            llm_evaluator_fail_response=llm_evaluator_fail_response,
            llm_evaluator_pass_response=llm_evaluator_pass_response,
            llm_callable=llm_callable, 
            **kwargs)
        self._llm_evaluator_prompt_generator = eval_llm_prompt_generator
        self._llm_callable = llm_callable
        self._fail_response = llm_evaluator_fail_response
        self._pass_response = llm_evaluator_pass_response

    def get_llm_response(self, prompt: str) -> str:
        """Gets the response from the LLM.

        Args:
            prompt (str): The prompt to send to the LLM.

        Returns:
            str: The response from the LLM.
        """
        # 0. Create messages
        messages = [{"content": prompt, "role": "user"}]
        
        # 0b. Setup auth kwargs if the model is from OpenAI
        kwargs = {}
        _model, provider, *_rest = get_llm_provider(self._llm_callable)
        if provider == "openai":
            kwargs["api_key"] = get_call_kwarg("api_key") or os.environ.get("OPENAI_API_KEY")

        # 1. Get LLM response
        # Strip whitespace and convert to lowercase
        try:
            response = completion(model=self._llm_callable, messages=messages, **kwargs)
            response = response.choices[0].message.content  # type: ignore
            response = response.strip().lower()
        except Exception as e:
            raise RuntimeError(f"Error getting response from the LLM: {e}") from e

        # 3. Return the response
        return response

    def validate(self, value: Any, metadata: Dict) -> ValidationResult:
        """
        Validates is based on the relevance of the reference text to the original question.

        Args:
            value (Any): The value to validate. It must contain 'original_prompt' and 'reference_text' keys.
            metadata (Dict): The metadata for the validation.
                user_message: Required key. User query passed into RAG LLM.
                context: Required key. Context used by RAG LLM.
                llm_response: Optional key. By default, the gaurded LLM will make the RAG LLM call, which corresponds
                    to the `value`. If the user calls the guard with on="prompt", then the original RAG LLM response
                    needs to be passed into the guard as metadata for the LLM judge to evaluate.

        Returns:
            ValidationResult: The result of the validation. It can be a PassResult if the reference 
                              text is relevant to the original question, or a FailResult otherwise.
        """
        # 1. Get the question and arg from the value
        user_input_message = metadata.get("user_message")
        if user_input_message is None:
            raise RuntimeError(
                "original_prompt missing from value. "
                "Please provide the original prompt."
            )

        reference_text = metadata.get("context")
        if reference_text is None:
            raise RuntimeError(
                "'reference_text' missing from value. "
                "Please provide the reference text."
            )
        
        # Option to override guarded LLM call with response passed in through metadata
        if metadata.get("llm_response") is not None:
            value = metadata.get("llm_response")

        # 2. Setup the prompt
        prompt = self._llm_evaluator_prompt_generator.generate_prompt(user_input_message=user_input_message, reference_text=reference_text, llm_response=value)
        logging.debug(f"evaluator prompt: {prompt}")

        # 3. Get the LLM response
        llm_response = self.get_llm_response(prompt)
        logging.debug(f"llm evaluator response: {llm_response}")

        # 4. Check the LLM response and return the result
        if llm_response == self._fail_response:
            return FailResult(error_message=f"The LLM says {self._fail_response}. The validation failed.")

        if llm_response == self._pass_response:
            return PassResult()

        return FailResult(
            error_message="The LLM returned an invalid answer. Failing the validation..."
        )
