
import os 
import pdb 
import json
from re import L
from string import whitespace
import torch 
import logging
import collections

from collections import defaultdict

from typing import List
from tqdm import tqdm 
from torch.utils.data import Dataset
from .input_utils import get_start_poses, check_if_start, get_word_position
from .mrc_converter import read_query_templates
from .seq2seq_utils import prepare_output


logger = logging.getLogger(__name__)


class InputExample(object):
    """A single training/test example for event extraction."""

    def __init__(self, example_id, text, pred_type, true_type, 
                input_template=None, 
                trigger_left=None, 
                trigger_right=None, 
                argu_left=None, 
                argu_right=None, 
                labels=None):
        """Constructs a InputExample.

        Args:
            example_id: Unique id for the example.
            text: List of str. The untokenized text.
            triggerL: Left position of trigger.
            triggerR: Light position of tigger.
            labels: Event type of the trigger
        """
        self.example_id = example_id
        self.text = text
        self.pred_type = pred_type
        self.true_type = true_type
        self.input_template = input_template
        self.trigger_left = trigger_left 
        self.trigger_right = trigger_right
        self.argu_left = argu_left
        self.argu_right = argu_right
        self.labels = labels


class InputFeatures(object):
    """Input features of an instance."""
    
    def __init__(self,
                 example_id,
                 input_ids,
                 attention_mask,
                 token_type_ids=None,
                 labels=None,
                 start_positions=None,
                 end_positions=None
        ):
        self.example_id = example_id
        self.input_ids = input_ids
        self.attention_mask = attention_mask
        self.token_type_ids = token_type_ids
        self.labels = labels
        self.start_positions = start_positions
        self.end_positions = end_positions


class DataProcessor(Dataset):
    """Base class of data processor."""

    def __init__(self, config, tokenizer, pred_file, is_training):
        self.config = config
        self.tokenizer = tokenizer
        self.is_training = is_training
        self.config.role2id["X"] = -100
        self.examples = []
        self.input_features = []
        # data for trainer evaluation 
        self.data_for_evaluation = {}
        # event prediction file path 
        if pred_file is not None:
            if not os.path.exists(pred_file):
                logger.warning("%s doesn't exist.We use golden triggers" % pred_file)
                self.event_preds = None 
            else:
                self.event_preds = json.load(open(pred_file))
        else:
            logger.warning("Event predictions is none! We use golden triggers.")
            self.event_preds = None 
        
    def read_examples(self, input_file):
        raise NotImplementedError

    def convert_examples_to_features(self):
        raise NotImplementedError

    def get_data_for_evaluation(self):
        self.data_for_evaluation["pred_types"] = self.get_pred_types()
        self.data_for_evaluation["true_types"] = self.get_true_types()
        return self.data_for_evaluation

    def get_pred_types(self):
        pred_types = []
        for example in self.examples:
            pred_types.append(example.pred_type)
        return pred_types 

    def get_true_types(self):
        true_types = []
        for example in self.examples:
            true_types.append(example.true_type)
        return true_types

    def _truncate(self, outputs, max_seq_length):
        is_truncation = False 
        if len(outputs["input_ids"]) > max_seq_length:
            print("An instance exceeds the maximum length.")
            is_truncation = True 
            for key in ["input_ids", "attention_mask", "token_type_ids", "offset_mapping"]:
                if key not in outputs:
                    continue
                outputs[key] = outputs[key][:max_seq_length]
        return outputs, is_truncation

    def get_ids(self):
        ids = []
        for example in self.examples:
            ids.append(example.example_id)
        return ids 

    def __len__(self):
        return len(self.input_features)

    def __getitem__(self, index):
        features = self.input_features[index]
        data_dict = dict(
            input_ids = torch.tensor(features.input_ids, dtype=torch.long),
            attention_mask = torch.tensor(features.attention_mask, dtype=torch.float32)
        )
        if features.token_type_ids is not None and self.config.return_token_type_ids:
            data_dict["token_type_ids"] = torch.tensor(features.token_type_ids, dtype=torch.long)
        if features.labels is not None:
            data_dict["labels"] = torch.tensor(features.labels, dtype=torch.long)
        if features.start_positions is not None: 
            data_dict["start_positions"] = torch.tensor(features.start_positions, dtype=torch.long)
        if features.end_positions is not None:
            data_dict["end_positions"] = torch.tensor(features.end_positions, dtype=torch.long)
        return data_dict
        
    def collate_fn(self, batch):
        output_batch = dict()
        for key in batch[0].keys():
            output_batch[key] = torch.stack([x[key] for x in batch], dim=0)
        if self.config.truncate_in_batch:
            input_length = int(output_batch["attention_mask"].sum(-1).max())
            for key in ["input_ids", "attention_mask", "token_type_ids", "trigger_left_mask", "trigger_right_mask"]:
                if key not in output_batch:
                    continue
                output_batch[key] = output_batch[key][:, :input_length]
            if "labels" in output_batch and len(output_batch["labels"].shape) == 2:
                if self.config.truncate_seq2seq_output:
                    output_length = int((output_batch["labels"]!=-100).sum(-1).max())
                    output_batch["labels"] = output_batch["labels"][:, :output_length]
                else:
                    output_batch["labels"] = output_batch["labels"][:, :input_length] 
        return output_batch


class TCProcessor(DataProcessor):
    """Data processor for token classification."""

    def __init__(self, config, tokenizer, input_file, pred_file, is_training=False):
        super().__init__(config, tokenizer, pred_file, is_training)
        self.read_examples(input_file)
        self.convert_examples_to_features()

    def read_examples(self, input_file):
        self.examples = []
        trigger_idx = 0
        with open(input_file, "r") as f:
            all_lines = f.readlines()
            for line in tqdm(all_lines, desc="Reading from %s" % input_file):
                item = json.loads(line.strip())
                # training and valid set
                for event in item["events"]:
                    for trigger in event["triggers"]:
                        argu_for_trigger = set()
                        if self.event_preds is not None \
                            and not self.config.golden_trigger \
                            and not self.is_training:    
                            pred_event_type = self.event_preds[trigger_idx] 
                        else:
                            pred_event_type = event["type"]
                        for argument in trigger["arguments"]:
                            for mention in argument["mentions"]:
                                example = InputExample(
                                    example_id=trigger["id"],
                                    text=item["text"],
                                    pred_type=pred_event_type,
                                    true_type=event["type"],
                                    trigger_left=trigger["position"][0],
                                    trigger_right=trigger["position"][1],
                                    argu_left=mention["position"][0],
                                    argu_right=mention["position"][1],
                                    labels=argument["role"]
                                )
                                argu_for_trigger.add(mention['mention_id'])
                                self.examples.append(example)
                        for entity in item["entities"]:
                            # check whether the entity is an argument 
                            is_argument = False 
                            for mention in entity["mentions"]:
                                if mention["mention_id"] in argu_for_trigger:
                                    is_argument = True 
                                    break 
                            if is_argument:
                                continue
                            # negative arguments 
                            for mention in entity["mentions"]:
                                example = InputExample(
                                    example_id=trigger["id"],
                                    text=item["text"],
                                    pred_type=pred_event_type,
                                    true_type=event["type"],
                                    trigger_left=trigger["position"][0],
                                    trigger_right=trigger["position"][1],
                                    argu_left=mention["position"][0],
                                    argu_right=mention["position"][1],
                                    labels="NA"
                                )
                                if "train" in input_file or self.config.golden_trigger:
                                    example.pred_type = event["type"]
                                self.examples.append(example)
                        trigger_idx += 1
                # negative triggers 
                for neg in item["negative_triggers"]:
                    trigger_idx += 1         

    def insert_marker(self, text, type, trigger_position, argument_position, markers, whitespace=True):
        markered_text = ""
        for i, char in enumerate(text):
            if i == trigger_position[0]:
                markered_text += markers[type][0]
                markered_text += " " if whitespace else ""
            if i == argument_position[0]:
                markered_text += markers["argument"][0]
                markered_text += " " if whitespace else ""
            markered_text += char 
            if i == trigger_position[1]-1:
                markered_text += " " if whitespace else ""
                markered_text += markers[type][1]
            if i ==argument_position[1]-1:
                markered_text += " " if whitespace else ""
                markered_text += markers["argument"][1]
        return markered_text

    def convert_examples_to_features(self): 
        # merge and then tokenize
        self.input_features = []
        whitespace = True if self.config.language == "English" else False 
        for example in tqdm(self.examples, desc="Processing features for TC"):
            text = self.insert_marker(example.text, 
                                        example.pred_type,
                                        [example.trigger_left, example.trigger_right], 
                                        [example.argu_left, example.argu_right], 
                                        self.config.markers, 
                                        whitespace)
            outputs = self.tokenizer(text, 
                                    padding="max_length",
                                    truncation=True,
                                    max_length=self.config.max_seq_length)
            is_overflow = False 
            try:
                left = outputs["input_ids"].index(self.tokenizer.convert_tokens_to_ids(self.config.markers["argument"][0]))
                right = outputs["input_ids"].index(self.tokenizer.convert_tokens_to_ids(self.config.markers["argument"][1]))
            except: 
                logger.warning("Markers are not in the input tokens.")
                is_overflow = True
            # Roberta tokenizer doesn't return token_type_ids
            if "token_type_ids" not in outputs:
                outputs["token_type_ids"] = [0] * len(outputs["input_ids"])
                
            features = InputFeatures(
                example_id = example.example_id,
                input_ids = outputs["input_ids"],
                attention_mask = outputs["attention_mask"],
                token_type_ids = outputs["token_type_ids"]
            )
            if example.labels is not None:
                features.labels = self.config.role2id[example.labels]
                if is_overflow:
                    features.labels = -100
            self.input_features.append(features)


class SLProcessor(DataProcessor):
    """Data processor for sequence labeling."""

    def __init__(self, config, tokenizer, input_file, pred_file, is_training=False):
        super().__init__(config, tokenizer, pred_file, is_training)
        self.positive_candidate_indices = []
        self.is_overflow = []
        self.config.role2id["X"] = -100
        self.read_examples(input_file)
        self.convert_examples_to_features()

    def read_examples(self, input_file):
        self.examples = []
        trigger_idx = 0
        with open(input_file, "r", encoding="utf-8") as f:
            for line in tqdm(f.readlines(), desc="Reading from %s" % input_file):
                item = json.loads(line.strip())

                if self.config.language == "English":
                    words = item["text"].split()
                elif self.config.language == "Chinese":
                    words = list(item["text"])
                else:
                    raise NotImplementedError

                if "events" in item:
                    for event in item["events"]:
                        for trigger in event["triggers"]:
                            if self.event_preds is not None \
                                and not self.config.golden_trigger \
                                and not self.is_training:    
                                pred_event_type = self.event_preds[trigger_idx] 
                            else:
                                pred_event_type = event["type"]
                            if self.config.language == "English":
                                trigger_left = len(item["text"][:trigger["position"][0]].split())
                                trigger_right = len(item["text"][:trigger["position"][1]].split())
                            elif self.config.language == "Chinese":
                                trigger_left = trigger["position"][0]
                                trigger_right = trigger["position"][1]
                            else:
                                raise NotImplementedError
                            labels = ["O"] * len(words)

                            for argument in trigger["arguments"]:
                                for mention in argument["mentions"]:
                                    if self.config.language == "English":
                                        left_pos = len(item["text"][:mention["position"][0]].split())
                                        right_pos = len(item["text"][:mention["position"][1]].split())
                                    elif self.config.language == "Chinese":
                                        left_pos = mention["position"][0]
                                        right_pos = mention["position"][1]
                                    else:
                                        raise NotImplementedError

                                    labels[left_pos] = f"B-{argument['role']}"
                                    for i in range(left_pos + 1, right_pos):
                                        labels[i] = f"I-{argument['role']}"

                            example = InputExample(
                                example_id=item["id"],
                                text=words,
                                pred_type=pred_event_type,
                                true_type=event["type"],
                                trigger_left=trigger_left,
                                trigger_right=trigger_right,
                                labels=labels,
                            )
                            trigger_idx += 1
                            self.examples.append(example)
                    # negative triggers
                    for neg in item["negative_triggers"]:
                        trigger_idx += 1
                else:
                    for candi in item["candidates"]:
                        if self.config.language == "English":
                            trigger_left = len(item["text"][:candi["position"][0]].split())
                            trigger_right = len(item["text"][:candi["position"][1]].split())
                        elif self.config.language == "Chinese":
                            trigger_left = candi["position"][0]
                            trigger_right = candi["position"][1]
                        else:
                            raise NotImplementedError
                        labels = ["O"] * len(words)

                        pred_event_type = self.event_preds[trigger_idx]
                        if pred_event_type != "NA":
                            example = InputExample(
                                example_id=item["id"],
                                text=words,
                                pred_type=pred_event_type,
                                true_type="NA",   # true type not given, set to NA.
                                trigger_left=trigger_left,
                                trigger_right=trigger_right,
                                labels=labels,
                            )
                            self.examples.append(example)
                            self.positive_candidate_indices.append(trigger_idx)

                        trigger_idx += 1

    def get_final_labels(self, labels, word_ids_of_each_token, label_all_tokens=False):
        final_labels = []
        pre_word_id = None
        for word_id in word_ids_of_each_token:
            if word_id is None:
                final_labels.append(-100)
            elif word_id != pre_word_id:  # first split token of a word
                final_labels.append(self.config.role2id[labels[word_id]])
            else:
                final_labels.append(self.config.role2id[labels[word_id]] if label_all_tokens else -100)
            pre_word_id = word_id

        return final_labels

    @staticmethod
    def insert_marker(text, event_type, labels, trigger_pos, markers):
        left, right = trigger_pos

        marked_text = text[:left] + [markers[event_type][0]] + text[left:right] + [markers[event_type][1]] + text[right:]
        marked_labels = labels[:left] + ["X"] + labels[left:right] + ["X"] + labels[right:]

        assert len(marked_text) == len(marked_labels)
        return marked_text, marked_labels

    def convert_examples_to_features(self):
        self.input_features = []
        self.is_overflow = []

        for example in tqdm(self.examples, desc="Processing features for SL"):
            text, labels = self.insert_marker(example.text,
                                              example.pred_type,
                                              example.labels,
                                              [example.trigger_left, example.trigger_right],
                                              self.config.markers)
            outputs = self.tokenizer(text,
                                     padding="max_length",
                                     truncation=False,
                                     max_length=self.config.max_seq_length,
                                     is_split_into_words=True)
            # Roberta tokenizer doesn't return token_type_ids
            if "token_type_ids" not in outputs:
                outputs["token_type_ids"] = [0] * len(outputs["input_ids"])
            outputs, is_overflow = self._truncate(outputs, self.config.max_seq_length)
            self.is_overflow.append(is_overflow)

            word_ids_of_each_token = outputs.word_ids()[: self.config.max_seq_length]
            final_labels = self.get_final_labels(labels, word_ids_of_each_token, label_all_tokens=False)

            features = InputFeatures(
                example_id=example.example_id,
                input_ids=outputs["input_ids"],
                attention_mask=outputs["attention_mask"],
                token_type_ids=outputs["token_type_ids"],
                labels=final_labels
            )
            self.input_features.append(features)


class Seq2SeqProcessor(DataProcessor):
    "Data processor for sequence to sequence."

    def __init__(self, config, tokenizer, input_file, pred_file, is_training=False):
        super().__init__(config, tokenizer, pred_file, is_training)
        self.read_examples(input_file)
        self.convert_examples_to_features()
    
    def read_examples(self, input_file):
        self.examples = []
        self.data_for_evaluation["golden_arguments"] = []
        trigger_idx = 0
        templates = json.load(open(self.config.template_file))
        self.data_for_evaluation["roles"] = []
        with open(input_file, "r", encoding="utf-8") as f:
            for line in tqdm(f.readlines(), desc="Reading from %s" % input_file):
                item = json.loads(line.strip())
                for event in item["events"]:
                    for trigger in event["triggers"]:
                        if self.event_preds is not None \
                            and not self.config.golden_trigger \
                            and not self.is_training:    
                            pred_event_type = self.event_preds[trigger_idx] 
                        else:
                            pred_event_type = event["type"]
                        if pred_event_type == "NA":
                            continue
                        arguments_per_trigger = defaultdict(list)
                        for argument in trigger["arguments"]:
                            for mention in argument["mentions"]:
                                arguments_per_trigger[argument["role"]].append(mention["mention"])
                        self.data_for_evaluation["golden_arguments"].append(dict(arguments_per_trigger))
                        self.data_for_evaluation["roles"].append(templates[pred_event_type]["roles"])
                        input_template = templates[pred_event_type]["template"]
                        output = prepare_output(arguments_per_trigger, input_template)
                        example = InputExample(
                            example_id=trigger["id"],
                            text=item["text"],
                            pred_type=pred_event_type,
                            true_type=event["type"],
                            input_template=input_template,
                            trigger_left=trigger["position"][0],
                            trigger_right=trigger["position"][1],
                            labels=output
                        )
                        if "train" in input_file or self.config.golden_trigger:
                            example.pred_type = event["type"]
                        trigger_idx += 1
                        self.examples.append(example)
                # negative triggers 
                for neg in item["negative_triggers"]:
                    trigger_idx += 1


    def insert_marker(self, text, type, trigger_pos, markers, whitespace=True):
        space = " " if whitespace else ""
        markered_text = ""
        tokens = text.split()
        char_pos = 0
        for i, token in enumerate(tokens):
            if char_pos == trigger_pos[0]:
                markered_text += markers[type][0] + space
            char_pos += len(token) + len(space)
            markered_text += token + space
            if char_pos == trigger_pos[1] + len(space):
                markered_text += markers[type][1] + space
        markered_text = markered_text.strip()
        return markered_text
        
    def convert_examples_to_features(self):
        self.input_features = []
        whitespace = True if self.config.language == "English" else False 
        for example in tqdm(self.examples, desc="Processing features for SL"):
            # template 
            input_template = self.tokenizer(example.input_template, 
                                            truncation=True,
                                            max_length=self.config.max_seq_length)
            # context 
            text = self.insert_marker(example.text, 
                                      example.true_type, 
                                      [example.trigger_left, example.trigger_right],
                                      self.config.markers,
                                      whitespace)
            input_context = self.tokenizer(text,
                                           truncation=True,
                                           padding="max_length",
                                           max_length=self.config.max_seq_length)
            # concatnate 
            input_ids = input_template["input_ids"] + input_context["input_ids"]
            attention_mask = input_template["attention_mask"] + input_context["attention_mask"]
            # truncation
            input_ids = input_ids[:self.config.max_seq_length]
            attention_mask = attention_mask[:self.config.max_seq_length]

            # output labels
            label_outputs = self.tokenizer(example.labels,
                                           padding="max_length",
                                           truncation=True,
                                           max_length=self.config.max_out_length)
            # set -100 to unused token 
            for i, flag in enumerate(label_outputs["attention_mask"]):
                if flag == 0:
                    label_outputs["input_ids"][i] = -100
            features = InputFeatures(
                example_id = example.example_id,
                input_ids = input_ids,
                attention_mask = attention_mask,
                labels = label_outputs["input_ids"]
            )
            self.input_features.append(features)


class MRCProcessor(DataProcessor):
    "Data processor for machine reading comprehension."

    def __init__(self, config, tokenizer, input_file, pred_file, is_training=False):
        super().__init__(config, tokenizer, pred_file, is_training)
        self.read_examples(input_file)
        self.convert_examples_to_features()

    def read_examples(self, input_file):
        self.examples = []
        self.data_for_evaluation["golden_arguments"] = []
        trigger_idx = 0
        query_templates = read_query_templates(self.config.prompt_file)
        template_id = 3
        with open(input_file, "r", encoding="utf-8") as f:
            for line in tqdm(f.readlines(), desc="Reading from %s" % input_file):
                item = json.loads(line.strip())
                for event in item["events"]:
                    for trigger in event["triggers"]:
                        if self.event_preds is not None \
                            and not self.config.golden_trigger \
                            and not self.is_training:    
                            pred_event_type = self.event_preds[trigger_idx] 
                        else:
                            pred_event_type = event["type"]
                        for role in query_templates[pred_event_type].keys():
                            query = query_templates[pred_event_type][role][template_id]
                            query = query.replace("[trigger]", trigger["trigger_word"])
                            if self.is_training:
                                no_answer = True 
                                for argument in trigger["arguments"]:
                                    if argument["role"] != role:
                                        continue
                                    no_answer = False
                                    for mention in argument["mentions"]:
                                        example = InputExample(
                                            example_id=trigger["id"],
                                            text=item["text"],
                                            pred_type=pred_event_type,
                                            true_type=event["type"],
                                            input_template=query,
                                            trigger_left=trigger["position"][0],
                                            trigger_right=trigger["position"][1],
                                            argu_left=mention["position"][0],
                                            argu_right=mention["position"][1]-1
                                        )
                                        self.examples.append(example)
                                if no_answer:
                                    example = InputExample(
                                        example_id=trigger["id"],
                                        text=item["text"],
                                        pred_type=pred_event_type,
                                        true_type=event["type"],
                                        input_template=query,
                                        trigger_left=trigger["position"][0],
                                        trigger_right=trigger["position"][1],
                                        argu_left=-1,
                                        argu_right=-1
                                    )
                                    self.examples.append(example)
                            else:
                                # golden label
                                key = str(item["id"]) + "_" + trigger["id"]
                                arguments_per_trigger = dict(id=key, role=role, arguments=[])
                                arguments_per_trigger["pred_type"] = pred_event_type
                                arguments_per_trigger["true_type"] = event["type"]
                                for argument in trigger["arguments"]:
                                    if argument["role"] == role:
                                        arguments_per_trigger["arguments"].append(argument)
                                self.data_for_evaluation["golden_arguments"].append(arguments_per_trigger)
                                # one instance per query 
                                example = InputExample(
                                    example_id=trigger["id"],
                                    text=item["text"],
                                    pred_type=pred_event_type,
                                    true_type=event["type"],
                                    input_template=query,
                                    trigger_left=trigger["position"][0],
                                    trigger_right=trigger["position"][1]
                                )
                                self.examples.append(example)
                        trigger_idx += 1
                # negative triggers 
                for neg in item["negative_triggers"]:
                    trigger_idx += 1  


    def word_offset_to_subword_offset(self, position, offsets):
        for i, offset in enumerate(offsets):
            if offset[0] == position:
                return i 
        return -1
    

    def subword_offset_to_word_offset(self, offset_mapping, text, base):
        subword_to_word = dict()
        for i, offset in enumerate(offset_mapping):
            if offset[0] == offset[1]:
                subword_to_word[i+base] = -1
            else:
                word_pos = len(text[:offset[0]].split())
                subword_to_word[i+base] = word_pos
        return subword_to_word


    def convert_examples_to_features(self):
        self.input_features = []
        self.data_for_evaluation["text_range"] = []
        self.data_for_evaluation["subword_to_word"] = []
        self.data_for_evaluation["text"] = []
        whitespace = True if self.config.language == "English" else False 
        for example in tqdm(self.examples, desc="Processing features for MRC"):
            # template 
            input_template = self.tokenizer(example.input_template, 
                                            truncation=True,
                                            max_length=self.config.max_seq_length)
            # context 
            input_context = self.tokenizer(example.text,
                                           truncation=True,
                                           padding="max_length",
                                           max_length=self.config.max_seq_length,
                                           return_offsets_mapping=True)
            # concatnate 
            input_ids = input_template["input_ids"] + input_context["input_ids"][1:]
            attention_mask = input_template["attention_mask"] + input_context["attention_mask"][1:]
            # truncation
            input_ids = input_ids[:self.config.max_seq_length]
            attention_mask = attention_mask[:self.config.max_seq_length]
            # output labels
            template_offset = len(input_template["input_ids"])
            offsets = input_context["offset_mapping"][1:]
            start_position = self.word_offset_to_subword_offset(example.argu_left, offsets)
            end_position = self.word_offset_to_subword_offset(example.argu_right, offsets)
            start_position = 0 if start_position == -1 else start_position + template_offset
            end_position = 0 if end_position == -1 else end_position + template_offset
            # data for evaluation
            text_range = dict()
            text_range["start"] = len(input_template["input_ids"])
            text_length = len(self.tokenizer.tokenize(example.text))
            text_range["end"] = text_range["start"] + text_length
            subword_to_word = self.subword_offset_to_word_offset(offsets, example.text, text_range["start"])
            self.data_for_evaluation["text_range"].append(text_range)
            self.data_for_evaluation["subword_to_word"].append(subword_to_word)
            self.data_for_evaluation["text"].append(example.text)
            # features
            features = InputFeatures(
                example_id = example.example_id,
                input_ids = input_ids,
                attention_mask = attention_mask,
                start_positions=start_position,
                end_positions=end_position
            )
            self.input_features.append(features)
            

            

