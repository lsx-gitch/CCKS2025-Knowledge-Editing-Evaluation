from copy import deepcopy
from typing import Any, Dict, List, Tuple
from peft import get_peft_model, AdaLoraConfig, TaskType, get_peft_model_state_dict, set_peft_model_state_dict, LoraConfig
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoProcessor

from .lora_hparams import LoRAHyperParams
from .lora_multimodal_hparams import LoRAMultimodalHyperParams


def apply_lora_to_model(
        model: AutoModelForCausalLM,
        tok: AutoTokenizer,
        requests: List[Dict],
        hparams: LoRAHyperParams,
        copy=False,
        return_orig_weights=False,
        keep_original_weight=False,
        **kwargs: Any,
) -> Tuple[AutoModelForCausalLM, Dict[str, Any]]:
    """
    Returns a model with the desired changes.
    :param copy: If true, will preserve the original model while creating a new one to edit.
        Note that you are responsible for deallocating the new model's memory to avoid leaks.
    :return: (1) the updated model, (2) the weights that changed
    """
    weights_copy = {}
    if copy:
        model = deepcopy(model)

    edited_model = execute_lora(model, tok, requests, hparams, keep_original_weight)

    return edited_model, weights_copy


def execute_lora(
        model: AutoModelForCausalLM,
        tok: AutoTokenizer,
        requests: List[Dict],
        hparams: LoRAHyperParams,
        keep_original_weight=False,
        **kwargs: Any,
) -> Dict[str, Tuple[torch.Tensor]]:
    """
    Executes the Lora update algorithm for the specified update at the specified layer
    Invariant: model at beginning of function == model at end of function
    """
    model.config.use_cache = False
    model.supports_gradient_checkpointing = True  #
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    if hparams.lora_type == "lora":
        Config = LoraConfig
    elif hparams.lora_type == "adalora":
        Config = AdaLoraConfig
    else:
        raise NotImplementedError
    if not keep_original_weight and hasattr(model,'peft_config'):
        peft_model = model
    else:
        peft_config = Config(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=hparams.rank,
            lora_alpha=hparams.lora_alpha, lora_dropout=hparams.lora_dropout,
            layers_to_transform=hparams.layers if len(hparams.layers) > 0 else None,
            target_modules=hparams.target_modules
        )
        peft_model = get_peft_model(model, peft_config)

    peft_model.is_parallelizable = True
    peft_model.model_parallel = True
    if hasattr(peft_model, 'print_trainable_parameters'):
        peft_model.print_trainable_parameters()
    requests = deepcopy(requests)
    for request in requests:
        if '{}' in request['prompt']:
            request['prompt'] = request['prompt'].format(request['subject'])
        print(
            f"Executing LoRA algo for: "
            f"[{request['prompt']}] -> [{request['target_new']}]"
        )
    device = torch.device('cpu')
    # Define inputs
    texts = [r["prompt"] for r in requests]
    targets = [r["target_new"] for r in requests]

    # Configure optimizer / gradients
    opt = torch.optim.Adam(
        peft_model.parameters(),
        lr=hparams.lr,
        weight_decay=hparams.weight_decay,
    )

    # if torch.__version__ >= "2" and sys.platform != "win32":
    #     model = torch.compile(model)
    loss_meter = AverageMeter()
    for it in range(hparams.num_steps):
        print(20 * "=")
        print(f"Epoch: {it}")
        print(20 * "=")
        loss_meter.reset()

        for txt, tgt in zip(
                chunks(texts, hparams.batch_size), chunks(targets, hparams.batch_size)
        ):
            mask_token = -100
            opt.zero_grad()
            if 't5' in hparams.model_name.lower():
                inputs = tok(txt, return_tensors="pt", padding=True).to(device)
                bs = inputs["input_ids"].shape[0]
                target_ids = tok(tgt, return_tensors="pt", padding=True)["input_ids"].to(
                    device
                )
                inputs['labels'] = target_ids
                logits = peft_model(**inputs).logits
                unmasked_log_probs = logits.log_softmax(-1).gather(-1, inputs['labels'].unsqueeze(-1)).squeeze(-1)
                mask = inputs['labels'] != -100
                n_tokens = mask.float().sum()
                avg_log_prob = (unmasked_log_probs * mask.float()).sum() / n_tokens
                nll = -avg_log_prob
                loss = nll
            else:
                # src_trg_inputs = tok(txt + tgt, return_tensors="pt", padding=True).to(device)
                # bs = src_trg_inputs["input_ids"].shape[0]
                # targ = deepcopy(src_trg_inputs['input_ids'])
                # pred = peft_model(**src_trg_inputs).logits
                # pred = pred[:, :-1]
                # targ = targ[:, 1:]
                # mask = targ != -100
                # n_tokens = mask.float().sum()
                # unmasked_log_probs = pred.log_softmax(-1).gather(-1, targ.unsqueeze(-1)).squeeze(-1)
                # log_prob = (unmasked_log_probs * mask.float()).sum() / n_tokens
                # loss = -log_prob
                # eos_token = tok.decode(tok.eos_token_id)
                full_prompt = [f"{p} {l}" for p, l in zip(txt, tgt)]
                prompt_ids = tok(list(txt), return_tensors="pt", padding=True, truncation=True)["input_ids"]
                num_prompt_toks = [int((i != tok.pad_token_id).sum()) for i in prompt_ids]
                tokens = tok(full_prompt, return_tensors="pt", padding=True, truncation=True)
                bs = tokens["input_ids"].shape[0]
                tokens["labels"] = tokens["input_ids"].clone()
                num_pad_toks = [int((i == tok.pad_token_id).sum()) for i in tokens["labels"]]
                for i in range(len(txt)):
                    tokens["labels"][i][num_pad_toks[i]:num_pad_toks[i]+num_prompt_toks[i]] = mask_token
                tokens["labels"][tokens["input_ids"] == tok.pad_token_id] = mask_token
                tokens = tokens.to(device)
                pred = peft_model(**tokens)
                loss = pred.loss
                # pred = peft_model(**tokens)
                # loss = pred.loss
                # targ = target_ids
                # pred = peft_model(**src_trg_inputs).logits
                # pred = pred[:, :-1]
                # pred = pred[:, -targ.size(1):]

                # mask = targ != -100
                # n_tokens = mask.float().sum()
                # unmasked_log_probs = pred.log_softmax(-1).gather(-1, targ.unsqueeze(-1)).squeeze(-1)
                # log_prob = (unmasked_log_probs * mask.float()).sum() / n_tokens
                # loss = -log_prob
            print(f"Batch loss {loss.item()}")
            loss_meter.update(loss.item(), n=bs)

            # if loss.item() >= 1e-3:
            loss.backward()
            opt.step()

        print(f"Total loss {loss_meter.avg}")

        # if loss_meter.avg < 1e-3:
        #     break
    return peft_model




def apply_lora_to_multimodal_model(
        model: AutoModelForCausalLM,
        tok: AutoProcessor,
        requests: List[Dict],
        hparams: LoRAMultimodalHyperParams,
        copy=False,
        return_orig_weights=False,
        keep_original_weight=False,
        **kwargs: Any,
) -> Tuple[AutoModelForCausalLM, Dict[str, Any]]:
    """
    Returns a model with the desired changes.
    :param copy: If true, will preserve the original model while creating a new one to edit.
        Note that you are responsible for deallocating the new model's memory to avoid leaks.
    :return: (1) the updated model, (2) the weights that changed
    """
    weights_copy = {}
    device = 'cpu'
    if copy:
        model = deepcopy(model)
        model.to(device)

    edited_model = execute_multimodal_lora(model, tok, requests, hparams, keep_original_weight)

    return edited_model, weights_copy

# import deepspeed

def execute_multimodal_lora(
        model: AutoModelForCausalLM,
        processor: AutoProcessor,
        requests: List[Dict],
        hparams: LoRAMultimodalHyperParams,
        keep_original_weight=False,
        **kwargs: Any,
) -> Dict[str, Tuple[torch.Tensor]]:
    """
    Executes the Lora update algorithm for the specified update at the specified layer
    Invariant: model at beginning of function == model at end of function
    """
    model.config.use_cache = False
    model.supports_gradient_checkpointing = True  #
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    
    if hparams.lora_type == "lora":
        Config = LoraConfig
    elif hparams.lora_type == "adalora":
        Config = AdaLoraConfig
    else:
        raise NotImplementedError
    if not keep_original_weight and hasattr(model,'peft_config'):
        peft_model = model
    else:
        peft_config = Config(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=hparams.rank,
            lora_alpha=hparams.lora_alpha, lora_dropout=hparams.lora_dropout,
            target_modules=hparams.target_modules
        )
        peft_model = get_peft_model(model, peft_config)

    peft_model.to(dtype=torch.float32)
    peft_model.is_parallelizable = True
    peft_model.model_parallel = True
    from torch.optim.lr_scheduler import ExponentialLR
    opt = torch.optim.SGD(
        peft_model.parameters(),
        lr=hparams.lr,
        weight_decay=hparams.weight_decay,
    )
    sheduler = ExponentialLR(opt, gamma=hparams.sh_lr)    
    if hasattr(peft_model, 'print_trainable_parameters'):
        peft_model.print_trainable_parameters()
    requests = deepcopy(requests)
    for request in requests:
        print(
            f"Executing LoRA algo for: "
            f"[{request['prompt']}] -> [{request['target']}]"
        )
    device = torch.device('cpu')
    # Define inputs
    prompts = [r["prompt"] for r in requests]
    labels = [r["target"] for r in requests]
    file_type = requests[0]['file_type']
    input_images = [r['image'] for r in requests]
    loss_meter = AverageMeter()
    
    for it in range(hparams.num_steps):
        print(20 * "=")
        print(f"Epoch: {it}")
        print(20 * "=")
        loss_meter.reset()

        for txt, tgt in zip(
                chunks(prompts, hparams.batch_size), chunks(labels, hparams.batch_size)
        ):
            mask_token = -100
            opt.zero_grad()
            
            if hasattr(hparams, 'use_chat_template') and hparams.use_chat_template:
                if file_type == "video":
                    temp_prompt = [processor.apply_chat_template([
                                            {

                                                "role": "user",
                                                "content": [
                                                    {"type": "video"},
                                                    {"type": "text", "text": p},
                                                    ],
                                            },
                                        ],
                                                        add_generation_prompt=True,
                                                        tokenize=False) + l
                                    for p, l in zip(prompts, labels)]
                    
                elif file_type in ["image", "single-image", "multi-image"]:
                    if file_type == "multi-image":
                        num_images = len(input_images[0])
                    else:
                        num_images = 1
                    
                    temp_prompt = [processor.apply_chat_template([
                                            {

                                                "role": "user",
                                                "content": [{"type": "image"}] * num_images + [{"type": "text", "text": p}],
                                            },
                                        ],
                                                        add_generation_prompt=True,
                                                        tokenize=False)  + l
                                    for p, l in zip(prompts, labels)]              
                else:
                    raise AssertionError("Not support file type: {}".format(file_type))
                
                full_prompt = temp_prompt
                if file_type in ["image", "single-image", "multi-image"]:
                    multimodal_inputs = processor(images=input_images, text=full_prompt, return_tensors="pt", padding=True).to(device, dtype=torch.float32)
                elif file_type == "video":
                    multimodal_inputs = processor(videos=input_images[0], text=full_prompt, return_tensors="pt", padding=True).to(device, dtype=torch.float32)
                    
                tokens = multimodal_inputs
                            
            targets = processor.tokenizer(labels[0], add_special_tokens=False,
                     return_tensors="pt", padding=True, max_length=multimodal_inputs["input_ids"].size(1))["input_ids"]
    
            labels_ids = torch.full_like(multimodal_inputs["input_ids"], -100)
            labels_ids[:, -targets.size(1):] = targets
            tokens["labels"] = labels_ids
            
            tokens = tokens.to(device)
            pred = peft_model(**tokens)
            loss = pred.loss
            print(f"Batch loss {loss.item()}")
            # loss_meter.update(loss.item(), n=bs)

            # if loss.item() >= 1e-3:
            loss.backward()
            opt.step()
            sheduler.step()

        print(f"Total loss {loss_meter.avg}")

        # if loss_meter.avg < 1e-3:
        #     break
    return peft_model







class AverageMeter:
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def chunks(arr, n):
    """Yield successive n-sized chunks from arr."""
    chunk = []
    for a in arr:
        chunk.append(a)
        if len(chunk) == n:
            yield chunk
            chunk = []
    if len(chunk) > 0:
        yield chunk
