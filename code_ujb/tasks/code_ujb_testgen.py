import os
import random
import re
import signal
import string
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
import xml.etree.ElementTree as ET

import chardet
import javalang
import numpy as np
from code_ujb.Task import Task, clean_signature
from datasets import load_dataset
from tqdm import tqdm
from transformers import AutoTokenizer

os.environ["TOKENIZERS_PARALLELISM"] = "true"

class StreamStopUJBTestGen():
    def __init__(self, function_signature, mode="complete"):
        self.function_signature = function_signature
        self.mode = mode
    
    def check_stop(self, generation):
        if self.mode == "complete":
            generation = self.function_signature + "{\n" + generation
        elif self.mode == "chat":
            if not self.function_signature in generation:
                return False
            generation = generation.split(self.function_signature)
            generation = generation[1]
            
        block_count, in_block, in_double_quote, in_single_quote = 0, False, False, False
        for char_idx in range(len(generation)):
            if generation[char_idx] == '"': in_double_quote = not in_double_quote
            if generation[char_idx] == "'": in_single_quote = not in_single_quote
            if generation[char_idx] == "{" and (not in_double_quote): 
                block_count += 1
                in_block = True
            if generation[char_idx] == "}" and (not in_double_quote): 
                block_count -= 1
            if block_count == 0 and in_block:
                return True
        return False

class CodeUJBTestGen(Task):
    """A task represents an entire benchmark including its dataset, problems,
    answers, generation settings and evaluation methods.
    """
    DATASET_PATH = "ZHENGRAN/code_ujb_testgen"

    def __init__(self):
        super().__init__(
            stop_words=["/**", "/**\n", "public", "private", "protected", "@Test", "    @Test", "\t@Test",
                        "\t/**", "\t/**\n", "\tpublic", "\tprivate", "\tprotected"],
            requires_execution=False,
        )
        print("Using Dataset:", self.DATASET_PATH)
        self.dataset = load_dataset(self.DATASET_PATH)
        self.tokenizer = AutoTokenizer.from_pretrained("gpt2")
        
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
        return StreamStopUJBTestGen(self.get_dataset()[idx]["function_signature"], mode=mode)
    
    def get_reference(self, doc):
        """Builds the reference solution for the doc (sample from the test dataset)."""
        return doc["function"]

    @staticmethod
    def _stop_at_stop_token(decoded_string, stop_tokens):
        """
        Produces the prefix of decoded_string that ends at the first occurrence of
        a stop_token.
        WARNING: the decoded_string *must not* include the prompt, which may have stop tokens
        itself.
        """
        min_stop_index = len(decoded_string)
        for stop_token in stop_tokens:
            stop_index = decoded_string.find(stop_token)
            if stop_index != -1 and stop_index < min_stop_index:
                min_stop_index = stop_index
        return decoded_string[:min_stop_index]

    
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
    
    def postprocess_generation_complete(self, generation, idx):
        """Defines the postprocessing for a LM generation.
        :param generation: str
            code generation from LM
        :param idx: int
            index of doc in the dataset to which the generation belongs
            (not used for Humaneval-Task)
        """
        prompt_complete_with_comment = self.dataset["train"][idx]["prompt_complete_with_comment"]
        generation = generation[len(prompt_complete_with_comment):]
        generation = self._stop_at_function(generation)
        # print("function", self.dataset["train"][idx]["function"])
        # print("generation", generation)
        return generation
    
    def postprocess_generation_chat(self, generation, idx):
        signature = self.dataset["train"][idx]["function_signature"]
        pre_signature, sub_signature = clean_signature(signature)
        if not sub_signature in generation:
            print("Can not find target function in answer!")
            return "Can not find target function in answer!\n\n"+generation
        generation = generation.split(sub_signature)
        # if len(generation) != 2:
        #     print("Multiple target function in answer!")
        #     return "Multiple target function in answer!\n\n"+generation
        generation = generation[1]
        function = self._stop_at_function(generation)
        
        generation = pre_signature + sub_signature + function
        return generation

    def evaluate(self, generations):
        """Takes the list of LM generations and evaluates them against ground truth references,
        returning the metric for the generations.
        :param generations: list(list(str))
            list of lists containing generations
        :param references: list(str)
            list of str containing refrences
        """
        all_tasks = []
        results = {"total": 0, "pass_syntax": {"count": 0}, "pass_compile": {"count": 0}, 
                   "pass_trigger": {"count": 0}, "line_coverage": [], "condition_coverage": [], 
                   "diff_coverage": [], "timed_out": 0, "detail": {}}
        total_tokens_dict = {}
        # generations = generations[-2:]
        for generation in tqdm(generations, total=len(generations)):
            idx = generation["task_idx"]
            gens = generation["outputs"]
            
            inps = generation["inputs"]
            rawouts = generation["raw_outputs"]
            inps_tokens = sum([len(tokens) for tokens in self.tokenizer.batch_encode_plus(inps, return_tensors="np")['input_ids']])
            rawouts_tokens = sum([len(tokens) for tokens in self.tokenizer.batch_encode_plus(rawouts, return_tensors="np")['input_ids']])
            total_tokens_dict[idx] = inps_tokens * 0.5 + rawouts_tokens
            
            project = self.dataset["train"][idx]["project"]
            bug_id = self.dataset["train"][idx]["bug_id"]
            bug_key = f"{project}-{bug_id}"
            testmethods = self.dataset["train"][idx]["testmethods"]
            # testmethods = testmethods[:1]
            source_dir = self.dataset["train"][idx]["source_dir"]
            start = self.dataset["train"][idx]["start"]
            end = self.dataset["train"][idx]["end"]
            location = self.dataset["train"][idx]["location"]
            source = self.dataset["train"][idx]["source"]
            # be_test_classes = self.dataset["train"][idx]["be_test_class_long_name"]
            be_test_classes = list(set([classmethod["be_test_class_name"] for classmethod in self.dataset["train"][idx]["classmethods"]]))
            
            one_tasks = [(idx, gen, project, bug_id, testmethods, source_dir, 
                      start, end, location, source,
                      be_test_classes) for gen in gens]
            all_tasks.extend(one_tasks)
        
        with ProcessPoolExecutor(max_workers=os.cpu_count()//4) as executor:
            # Submit all your tasks to the executor
            future_tasks = set()
            for task in all_tasks:
                future_tasks.add(executor.submit(validate_all_patches, task))
                time.sleep(0.01)
            # Use tqdm to display progress
            all_bug_results_list = []
            with tqdm(as_completed(future_tasks), total=len(all_tasks), desc="Evaluating all tasks...") as progress_bar:
                for future in progress_bar:
                    # Append the result to a list
                    all_bug_results_list.append(future.result())
        
        all_bug_results_dict = {}
        for bug_result in all_bug_results_list:
            if bug_result["idx"] not in all_bug_results_dict:
                all_bug_results_dict[bug_result["idx"]] = []
            all_bug_results_dict[bug_result["idx"]].append(bug_result)
        
        keys_list = list(all_bug_results_dict.keys())
        keys_list.sort()
        for idx in keys_list:
            bug_results = all_bug_results_dict[idx]
            
            example_detail = {"total": 0, "total_tokens": total_tokens_dict[idx], 
                              "pass_syntax": {"count": 0}, "pass_compile": {"count": 0}, 
                              "pass_trigger": {"count": 0}, "line_coverage": [], "condition_coverage": [], 
                              "diff_coverage": set(), "timed_out": 0}
            
            for detail in bug_results:
                example_detail["total"] += 1
                if detail["pass_syntax"]:
                    example_detail["pass_syntax"]["count"] += 1
                if detail["pass_compile"]:
                    example_detail["pass_compile"]["count"] += 1
                if detail["pass_trigger"]:
                    example_detail["pass_trigger"]["count"] += 1
                if detail["timed_out"]:
                    example_detail["timed_out"] += 1
                example_detail["line_coverage"].append(detail["line_coverage"])
                example_detail["condition_coverage"].append(detail["condition_coverage"])
                for line in detail["lines_coverage_info"]:
                    example_detail["diff_coverage"].add(line)
            
            for key in list(results.keys()):
                if not "pass" in key: continue
                for k in [1, 5, 10, 20, 100]:
                    if example_detail["total"] < k: continue 
                    example_detail[key][f"pass@k-{k}"] = get_pass_at_k(example_detail["total"], example_detail[key]["count"], k)
                    if not f"pass@k-{k}" in results[key]:
                        results[key][f"pass@k-{k}"] = []
                    results[key][f"pass@k-{k}"].append(example_detail[key][f"pass@k-{k}"])
                for t in [1000, 5000, 10000, 20000, 500000, 1000000]:
                    if example_detail["total_tokens"] < t: continue 
                    tk = t / (example_detail["total_tokens"] / example_detail["total"])
                    example_detail[key][f"pass@t-{t}"] = get_pass_at_k(example_detail["total"], example_detail[key]["count"], tk)
                    if not f"pass@t-{t}" in results[key]:
                        results[key][f"pass@t-{t}"] = []
                    results[key][f"pass@t-{t}"].append(example_detail[key][f"pass@t-{t}"])
                    
            example_detail["line_coverage"] = np.mean(example_detail["line_coverage"])
            example_detail["condition_coverage"] = np.mean(example_detail["condition_coverage"])
            example_detail["diff_coverage"] = list(example_detail["diff_coverage"])
            if len(example_detail["diff_coverage"]) == 0:
                example_detail["diff_coverage"] = 0
            else:
                example_detail["diff_coverage"] = \
                    len([x for x in example_detail["diff_coverage"] if x[-1] == True]) / len(example_detail["diff_coverage"])
                
            print(example_detail)
            results["detail"][idx] = example_detail
            results["total"] += 1
            if example_detail["pass_syntax"]["count"] > 0:
                results["pass_syntax"]["count"] += 1
            if example_detail["pass_compile"]["count"] > 0:
                results["pass_compile"]["count"] += 1
            if example_detail["pass_trigger"]["count"] > 0:
                results["pass_trigger"]["count"] += 1
            results["timed_out"] += example_detail["timed_out"]
            results["line_coverage"].append(example_detail["line_coverage"])
            results["condition_coverage"].append(example_detail["condition_coverage"])
            results["diff_coverage"].append(example_detail["diff_coverage"])
        
        for key in list(results.keys()):
            if not "pass" in key: continue
            for pkey in list(results[key].keys()):
                if "@" in pkey:
                    results[key][pkey] = np.mean(results[key][pkey])
        results["line_coverage"] = np.mean(results["line_coverage"])
        results["condition_coverage"] = np.mean(results["condition_coverage"])
        results["diff_coverage"] = np.mean(results["diff_coverage"])
        
        return results
    
def get_pass_at_k(n, c, k):
    if n - c < k : return 1.0
    return 1.0 - np.prod(1.0 - k / np.arange(n - c + 1, n + 1))

def read_file(file_path):
    with open(file_path, 'rb') as f:
        content = f.read()
    encoding = chardet.detect(content)['encoding']
    decoded_content = content.decode(encoding)
    return decoded_content

def save_file(file_path, content):
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(content)

def validate_all_patches(item):
    idx, patch, project, bug_id, testmethods, source_dir, start, end, location, source, be_test_classes = item
    def generate_random_string(length):
        characters = string.ascii_letters + string.digits  # 包含大写字母、小写字母和数字
        random_string = ''.join(random.choice(characters) for _ in range(length))
        return random_string

    tmp_folder = f"{project}-{bug_id}-" + generate_random_string(8)
    subprocess.run(['rm', '-rf', '/tmp/' + tmp_folder], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    cmd = ['defects4j', 'checkout', '-p', project, '-v', str(bug_id) + 'f', '-w', '/tmp/' + tmp_folder]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    source = source.split("\n")
    patch = patch.split("\n")
    source = "\n".join(source[:start] + patch + source[end+1:])

    save_file("/tmp/" + tmp_folder + "/" + location, source)
    
    compile_fail, timed_out, bugg, line_coverage, condition_coverage, syntax_error, lines_coverage_info\
        = run_d4j_test(source, testmethods, tmp_folder, be_test_classes)
        
    subprocess.run(['rm', '-rf', '/tmp/' + tmp_folder], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    return {
            "idx": idx,
            "project": project,
            "bug_id":  bug_id,
            "pass_syntax": not syntax_error,
            "pass_compile": not compile_fail,
            "pass_trigger": not bugg,
            "line_coverage": line_coverage,
            "condition_coverage": condition_coverage,
            "timed_out": timed_out,
            "lines_coverage_info": lines_coverage_info
        }
    
def run_d4j_test(source, testmethods, bug_id, be_test_classes):
    bugg = False
    compile_fail = False
    timed_out = False
    error_string = ""
    line_coverage = 0
    condition_coverage = 0
    lines_coverage_info = []

    try:
        tokens = javalang.tokenizer.tokenize(source)
        parser = javalang.parser.Parser(tokens)
        parser.parse()
    except:
        # print("Syntax Error")
        return True, False, True, line_coverage, condition_coverage, True, lines_coverage_info

    testmethod = testmethods[0]
    # print(testmethod.strip())
    cmd = 'defects4j test -w %s/ -t %s' % (('/tmp/' + bug_id), testmethod.strip())
    Returncode = ""
    error_file = open("/tmp/stderr.txt", "wb")
    # print(cmd)
    child = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=error_file, bufsize=-1,
                            start_new_session=True)
    while_begin = time.time()
    while True:
        Flag = child.poll()
        if Flag == 0:
            Returncode = child.stdout.readlines()  # child.stdout.read()
            # print(b"".join(Returncode).decode('utf-8'))
            error_file.close()
            break
        elif Flag != 0 and Flag is not None:
            compile_fail = True
            error_file.close()
            with open("/tmp/stderr.txt", "rb") as f:
                r = f.readlines()
            for line in r:
                if re.search(':\serror:\s', line.decode('utf-8')):
                    error_string = line.decode('utf-8')
                    break
            # print("error_string", error_string)
            break
        elif time.time() - while_begin > 120:
            error_file.close()
            os.killpg(os.getpgid(child.pid), signal.SIGTERM)
            timed_out = True
            break
        else:
            time.sleep(0.01)
    log = Returncode
    if len(log) > 0 and log[-1].decode('utf-8') == "Failing tests: 0\n":
        bugg = False
    else:
        bugg = True

    if not bugg:
        increment_path = os.path.join('/tmp/' + bug_id, "increment.txt")
        with open(increment_path, 'w') as f:
            f.writelines([file+"\n" for file in be_test_classes])
        cmd = ["defects4j", "coverage", "-w", ('/tmp/' + bug_id), "-t", testmethod.strip(), "-i", increment_path]
        # print(" ".join(cmd))
        child = subprocess.Popen(" ".join(cmd), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=-1,
                                start_new_session=True)
        Returncode = ""
        while_begin = time.time()
        while True:
            Flag = child.poll()
            if Flag == 0:
                Returncode = child.stdout.readlines()  # child.stdout.read()
                break
            elif Flag != 0 and Flag is not None:
                bugg = True
                break
            elif time.time() - while_begin > 180:
                os.killpg(os.getpgid(child.pid), signal.SIGTERM)
                bugg = True
                break
            else:
                time.sleep(0.01)
        log = Returncode
        if len(log) > 0:
            if "Line coverage:" in log[-2].decode('utf-8'):
                line_coverage = log[-2].decode('utf-8').split(":")[-1].strip().replace("%", "")
                line_coverage = float(line_coverage) / 100.0
            if "Condition coverage:" in log[-1].decode('utf-8'):
                condition_coverage = log[-1].decode('utf-8').split(":")[-1].strip().replace("%", "")
                condition_coverage = float(condition_coverage) / 100.0
            lines_coverage_info = analysis_coverage('/tmp/' + bug_id)
            
    bugg = bugg or line_coverage == 0
    return compile_fail, timed_out, bugg, line_coverage, condition_coverage, False, lines_coverage_info


def analysis_coverage(tmp_project_path):
    xml_file_path = os.path.join(tmp_project_path, "coverage.xml")

    # Load and parse the XML file
    tree = ET.parse(xml_file_path)
    root = tree.getroot()

    all_lines_info = []
    for package in root.findall(".//package"):
        for class_element in package.findall(".//class"):
            be_test_class_name = class_element.attrib["name"]
            be_test_class_file = class_element.attrib["filename"]
            # print(be_test_class_name, be_test_class_file)
            for line_element in class_element.findall(".//line"):
                # print(line_element.attrib["number"], line_element.attrib["hits"])
                all_lines_info.append((be_test_class_name, be_test_class_file, 
                                       line_element.attrib["number"],
                                       int(line_element.attrib["hits"])>0))

    return all_lines_info