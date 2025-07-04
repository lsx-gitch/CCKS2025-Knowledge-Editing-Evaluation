import json
from pathlib import Path

import torch
from torch.utils.data import Dataset
import typing
import transformers
from transformers import GPT2Tokenizer, GPT2TokenizerFast, LlamaTokenizer, AutoTokenizer

from ..util.globals import *
from ..trainer.utils import dict_to


class CKnowEditDataset(Dataset):

    def __init__(self, data_dir: str, size: typing.Optional[int] = None, config=None, *args, **kwargs):
        data_dir = Path(data_dir)
        Cknowedit_loc = data_dir

        if config is not None:
            self.config = config
        if config is not None and hasattr(config, 'max_length'):
            self.max_length = config.max_length
        else:
            self.max_length = 1000

        if config is not None and hasattr(config, 'tokenizer_name'):
            tok_name = (
                config.tokenizer_name
                if config.tokenizer_name is not None
                else config.model.name
            )
            tokenizer = getattr(transformers, config.tokenizer_class).from_pretrained(
                tok_name, trust_remote_code=True
            )
            if isinstance(tokenizer, GPT2Tokenizer) or isinstance(tokenizer, GPT2TokenizerFast):
                tokenizer.pad_token_id = tokenizer.eos_token_id
                tokenizer.padding_side = 'left'
                print('GPTTokenizer Detected, Set pad token id and left padding!!!')
            elif isinstance(tokenizer, LlamaTokenizer):
                tokenizer.pad_token_id = tokenizer.eos_token_id
                tokenizer.padding_side = 'left'
                print('LlamaTokenizer Detected, Set pad token id and left padding!!!')
            if 'qwen' in config.model_name.lower():
                tokenizer.eos_token='<|endoftext|>'
                tokenizer.pad_token='<|endoftext|>'
                tokenizer.unk_token='<|endoftext|>'
                tokenizer.padding_side = 'left'
                # print('QwenTokenizer Detected, Set pad token id and left padding!!!')
            self.tok = tokenizer

        with open(Cknowedit_loc, "r", encoding='utf-8') as f:
            raw = json.load(f)
  
        data = []
        for i, record in enumerate(raw):
            x = record['prompt']
            if("请填写下列古诗文的后一句：" in record['prompt']):
                subject = x.replace("请填写下列古诗文的后一句：",'')
            elif("请解释" in record['prompt'] and "中的意思。仅需给出该字意思即可，无需解释全文。" in record['prompt']):
                subject = x.replace("请解释",'').replace("中的意思。仅需给出该字意思即可，无需解释全文。",'')
            elif("请解释如下成语：" in record['prompt']):
                subject = x.replace("请解释如下成语：",'')
            elif("请给下面的字注音：" in record['prompt']):
                subject = x.replace("请给下面的字注音：",'')
            elif("请解释如下俗语或谚语：" in record['prompt']):
                subject = x.replace("请解释如下俗语或谚语：",'')
            else:
                subject = x
            data.append(
                {
                    "prompt": record["prompt"],
                    "target_new": record["target_new"],
                    "subject":subject,
                    "target_old": record["target_old"],
                    "portability": record["portability"] if "portability" in record else None,
                    "locality": record["locality"] if "locality" in record else None,
                    "rephrase":record["rephrase"] if "rephrase" in record else None
                }
            )

        if size is not None:
            data = data[:size]
        self._data = data

    def __getitem__(self, item):
        return self._data[item]

    def __len__(self):
        return len(self._data)

    def get_edit_labels(self, labels):
        return labels.masked_fill(labels == self.tok.pad_token_id, -100)

    def collate_fn(self, batch):
        src = [b["prompt"] for b in batch]
        trg = [b["target_new"] for b in batch]
        loc_data = [b["locality"] if len(b["locality"])!=0 else None for b in batch]
        loc=[l[0]["prompt"] if isinstance(l[0]["prompt"],str) else l[0]["prompt"] for l in loc_data]
        loc_ans = [l[0]["answer"] if isinstance(l[0]["answer"],str) else l[0]["answer"] for l in loc_data]

        batches = {
            f"{k1}_{k2}": v2
            for k1, v1 in {
                "src": src,
                "trg": trg,
            }.items()
            for k2, v2 in self.tok(
                v1,
                return_tensors="pt",
                padding=True,
                max_length=self.max_length,
                truncation=True,
            ).items()
        }

        batches["raw"] = batch

        # edit_inner
        edit_inner = {}
        edit_inner["input_ids"] = batches["src_input_ids"]
        edit_inner["attention_mask"] = batches["src_attention_mask"]
        edit_labels = self.get_edit_labels(batches["trg_input_ids"])

        edit_inner["labels"] = edit_labels

        # loc
        loc = dict(
            self.tok(
                loc,
                return_tensors="pt",
                padding=True,
                max_length=self.max_length,
                truncation=True,
            )
        )

        loc_ans = dict(
            self.tok(
                loc_ans,
                return_tensors="pt",
                padding=True,
                max_length=self.max_length,
                truncation=True,
            )
        )
        loc["decoder_attention_mask"] = loc_ans["attention_mask"]
        loc["labels"] = self.get_edit_labels(loc_ans["input_ids"])

        # portability TODO

        batch = {
            "edit_inner": edit_inner,
            "loc": loc,
            "raw": batch,
        }
        return dict_to(batch, self.config.device)

    def collate_gpt_fn(self, batch):
        src = [b["prompt"] for b in batch]
        trg = [b["target_new"] for b in batch]
        loc_data = [b["locality"] if len(b["locality"])!=0 else None for b in batch]
        loc=[l[0]["prompt"] if isinstance(l[0]["prompt"],str) else l[0]["prompt"] for l in loc_data]

        loc_ans = [l[0]["answer"] if isinstance(l[0]["answer"],str) else l[0]["answer"] for l in loc_data]
        loc_ans = [l if isinstance(l,str) else l[0] for l in loc_ans]

        src = [src_ + ' ' + trg_ for src_, trg_ in zip(src, trg)]
        loc = [loc_ + ' ' + loc_ans_ for loc_, loc_ans_ in zip(loc, loc_ans)]

        batches = {
            f"{k1}_{k2}": v2
            for k1, v1 in {
                "src": src,
                "trg": trg,
            }.items()
            for k2, v2 in self.tok(
                v1,
                return_tensors="pt",
                padding=True,
                max_length=self.max_length,
                truncation=True,
            ).items()
        }

        batches["raw"] = batch

        # edit_inner
        edit_inner = {}
        edit_inner["input_ids"] = batches["src_input_ids"]
        edit_inner["attention_mask"] = batches["src_attention_mask"]
        edit_labels = self.get_edit_labels(batches["trg_input_ids"])

        edit_inner["labels"] = edit_labels


        # loc
        loc = dict(
            self.tok(
                loc,
                return_tensors="pt",
                padding=True,
                max_length=self.max_length,
                truncation=True,
            )
        )

        loc_ans = dict(
            self.tok(
                loc_ans,
                return_tensors="pt",
                padding=True,
                max_length=self.max_length,
                truncation=True,
            )
        )
        loc["decoder_attention_mask"] = loc_ans["attention_mask"]
        loc["labels"] = self.get_edit_labels(loc_ans["input_ids"])

        # portability TODO
        batch = {
            "edit_inner": edit_inner,
            "loc": loc,
            "raw": batch,
        }
        return dict_to(batch, self.config.device)
    
