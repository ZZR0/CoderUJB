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
import re
import numpy as np
from tqdm import tqdm
from pathlib import Path

from code_ujb.Task import Task, clean_signature
from datasets import load_dataset
from code_ujb.tasks.custom_metrics.multiple_metrics.evaluation import evaluate_problem

os.environ["TOKENIZERS_PARALLELISM"] = "true"

class StreamStopUJBComplete():
    def __init__(self, function_signature, mode="complete"):
        self.function_signature = function_signature
        self.mode = mode
    
    def check_stop(self, generation):
        return False

class MultiplePython(Task):
    """A task represents an entire benchmark including its dataset, problems,
    answers, generation settings and evaluation methods.
    """
    DATASET_PATH = "ZHENGRAN/multiple-python"

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

    def get_prompt_complete(self, doc):
        """Builds the prompt for the LM to generate from."""
        return doc["prompt_complete"].strip()
    
    def get_prompt_chat(self, doc):
        return doc["prompt_chat"].strip()
    
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

    def postprocess_generation_complete(self, generation, idx):
        """Defines the postprocessing for a LM generation.
        :param generation: str
            code generation from LM
        :param idx: int
            index of doc in the dataset to which the generation belongs
            (not used for Humaneval-Task)
        """
        # prompt = self.get_prompt(self.dataset["train"][idx])
        function_signature = self.dataset["train"][idx]["function_signature"]
        # print("prompt", prompt_with_signature)
        # print("generation", generation)
        prefix = generation.split(function_signature)[0]
        generation = generation.split(function_signature)[-1]
        generation = generation.split("\ndef ")[0]
        return prefix + function_signature + generation

    def postprocess_generation_chat(self, generation, idx):
        def extract_code_block(gen, pattern):
            try:
                code_block = re.findall(pattern, gen, re.DOTALL | re.IGNORECASE)[0]
                return code_block
            except (IndexError, TypeError):
                return None
        prompt = self.get_dataset()[idx]["prompt"].split(self.get_dataset()[idx]["function_signature"])
        import_str = ""
        if len(prompt) == 2:
            import_str = prompt[0]
        
        patterns = [
            r'```python\n(.*?)```',
            r'```\n(.*?)```',
            r'\[PYTHON\]\n(.*?)\[/PYTHON\]'
        ]
        
        for pattern in patterns:
            code_block = extract_code_block(generation, pattern)
            if code_block is not None:
                return import_str + code_block
        
        if generation is None:
            return "$ERROR$"
        return generation
    
    
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
        [os.remove(p) for p in Path(temp_dir).glob("*.json")]
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

        all_results = []
        # execute the problems to evaluate them
        max_workers = os.cpu_count()//2 if os.cpu_count() > 2 else 1
        for file in tqdm(list_files):
            one_results = evaluate_problem(temp_dir, file, max_workers)
            all_results.append(one_results)

        # compute pass@k scores
        result_array = np.array(
            [for_file(r, k=[1, 2, 5, 10, 20, 40, 50, 80, 100]) for r in all_results]
        )
        result = result_array.mean(axis=0)
        name = (
            temp_dir.split("/")[-1]
            if temp_dir.split("/")[-1] != ""
            else temp_dir.split("/")[-2]
        )
        results = {
            f"pass@{k}": v
            for k, v in zip([1, 2, 5, 10, 20, 40, 50, 80, 100], result)
            if k <= len(generations[0])
        }
        [os.remove(p) for p in Path(temp_dir).glob("*.json")]
        os.removedirs(temp_dir)
        return results