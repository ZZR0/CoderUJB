"""Evaluating Large Language Models Trained on Code
https://arxiv.org/abs/2107.03374

The HumanEval dataset released by OpenAI includes 164 programming problems with a function signature,
docstring, body, and several unit tests. 
They were handwritten to ensure not to be included in the training set of code generation models.

Homepage: https://github.com/openai/human-eval
"""

import json
import os
import tempfile
import numpy as np
from tqdm import tqdm
from pathlib import Path

from code_ujb.Task import Task, clean_signature
from datasets import load_dataset
from code_ujb.tasks.multiple_metrics.evaluation import evaluate_problem

os.environ["TOKENIZERS_PARALLELISM"] = "true"

class StreamStopUJBComplete():
    def __init__(self, function_signature, mode="complete"):
        self.function_signature = function_signature
        self.mode = mode
    
    def check_stop(self, generation):
        return False

class MultipleJava(Task):
    """A task represents an entire benchmark including its dataset, problems,
    answers, generation settings and evaluation methods.
    """
    
    DATASET_PATH = "ZHENGRAN/multiple-java"

    def __init__(self):
        super().__init__(
            stop_words=[],
            requires_execution=False,
        )
        print("Using Dataset:", self.DATASET_PATH)
        self.dataset = load_dataset(self.DATASET_PATH)

    def get_dataset(self):
        """Returns dataset for the task or an iterable of any object, that get_prompt can handle"""
        return self.dataset["train"]

    def get_prompt(self, doc, mode="complete"):
        """Builds the prompt for the LM to generate from."""
        if mode == "complete":
            prompt_key = "prompt_complete"
        elif mode == "chat":
            prompt_key = "prompt_chat"
        else:
            raise KeyError()
        return doc[prompt_key].strip()
    
    def get_prompt_byidx(self, idx, mode="complete"):
        """Builds the prompt for the LM to generate from."""
        return self.get_prompt(self.get_dataset()[idx], mode=mode)

    def get_id_byidx(self, idx):
        """Builds the prompt for the LM to generate from."""
        return self.get_dataset()[idx]["task_id"]
    
    def get_stream_stop(self, idx, mode="complete"):
        return StreamStopUJBComplete(self.get_dataset()[idx]["function_signature"], mode=mode)
    
    def get_reference(self, doc):
        """Builds the reference solution for the doc (sample from the test dataset)."""
        return doc["function"]

    @staticmethod
    def _stop_at_function(generation):
        block_count, in_block, in_double_quote, in_single_quote = 0, False, False, False
        char_idx = 0
        for char_idx in range(len(generation)):
            if generation[char_idx] == '"': in_double_quote = not in_double_quote
            if generation[char_idx] == "'": in_single_quote = not in_single_quote
            if generation[char_idx] == "{" and (not in_double_quote): 
                block_count += 1
                in_block = True
            if generation[char_idx] == "}" and (not in_double_quote): 
                block_count -= 1
            if block_count == 0 and in_block:
                break
        if char_idx:
            generation = generation[:char_idx+1]
        return generation

    def postprocess_complete_generations(self, generations, idx):
        return [self.postprocess_complete_generation(gen, idx) for gen in generations]
    
    def postprocess_chat_generations(self, generations, idx):
        return [self.postprocess_chat_generation(gen, idx) for gen in generations]
        
    def postprocess_complete_generation(self, generation, idx):
        """Defines the postprocessing for a LM generation.
        :param generation: str
            code generation from LM
        :param idx: int
            index of doc in the dataset to which the generation belongs
            (not used for Humaneval-Task)
        """
        prompt_with_comment = self.dataset["train"][idx]["prompt_complete_without_signature"]
        generation = generation[len(prompt_with_comment):]
        generation = self._stop_at_function(generation)
        # print(prompt_with_comment + "\n" + generation + "\n}")
        return prompt_with_comment + generation[:-1]

    def postprocess_chat_generation(self, generation, idx):
        signature = self.dataset["train"][idx]["function_signature"].strip()
        
        pre_signature, sub_signature = clean_signature(signature)
        # if not clean_code(signature) in clean_code(generation):
        if not sub_signature in generation:
            # print(signature[-1])
            # if idx == 2:
            # print(signature)
            # print(pre_signature, sub_signature)
            # print(generation)
            # exit()
            print("Can not find target function in answer!")
            return "Can not find target function in answer!\n\n"+generation
        generation = generation.split(sub_signature)
        # if len(generation) != 2:
        #     print("Multiple target function in answer!")
        #     return "Multiple target function in answer!\n\n"+generation
        generation = generation[1]
        function = self._stop_at_function(generation)
        
        generation = pre_signature + sub_signature +  function
        return self.dataset["train"][idx]["prompt_complete_without_signature"] + "\n" + generation[:-1]
        
    
    def evaluate(self, generations):
        def estimator(n: int, c: int, k: int) -> float:
            """
            Calculates 1 - comb(n - c, k) / comb(n, k).
            """
            if n - c < k:
                return 1.0
            return 1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1))

        def for_file(path):
            with open(path, "r") as f:
                data = json.load(f)
            n = len(data["results"])
            c = len(
                [True for r in data["results"] if r["status"] == "OK" and r["exit_code"] == 0]
            )
            return np.array([estimator(n, c, 1), estimator(n, c, 5), estimator(n, c, 10), estimator(n, c, 20), estimator(n, c, 100)])

        temp_dir = tempfile.gettempdir()
        [os.remove(p) for p in Path(temp_dir).glob("*.results.json")]
        list_files = []
        for generation in tqdm(generations, total=len(generations)):
            idx = generation["task_idx"]
            gens = generation["outputs"]
            name = self.dataset["train"][idx]["name"]
        
            problem = {
                "name": name,
                "language": self.dataset["train"][idx]["language"],
                "prompt": self.dataset["train"][idx]["prompt"],
                "completions": gens,
                "tests": self.dataset["train"][idx]["tests"],
            }
            # each problem is save in a json file
            temp_file_name = os.path.join(temp_dir, f"{name}.json")
            list_files.append(temp_file_name)
            with open(temp_file_name, "wt") as f:
                json.dump(problem, f)
        print(
            f"Saved {len(list_files)} problems in {temp_dir} for evaluation, each problem has {len(generations[0]['outputs'])} completions"
        )

        # execute the problems to evaluate them
        max_workers = os.cpu_count() - 1 if os.cpu_count() > 1 else 1
        for file in tqdm(list_files):
            evaluate_problem(temp_dir, file, max_workers)

        # compute pass@k scores
        result_array = np.array(
            [for_file(p) for p in Path(temp_dir).glob("*.results.json")]
        )
        result = result_array.mean(axis=0)
        name = (
            temp_dir.split("/")[-1]
            if temp_dir.split("/")[-1] != ""
            else temp_dir.split("/")[-2]
        )
        results = {
            f"pass@{k}": v
            for k, v in zip([1, 5, 10, 20, 100], result)
            if k <= len(generations[0]['outputs'])
        }
        return results