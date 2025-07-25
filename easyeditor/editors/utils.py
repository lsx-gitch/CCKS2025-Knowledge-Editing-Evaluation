from typing import Optional, Union, List, Tuple, Dict
import os
import json
import numpy as np
import random
import math

def _chunks(arr, n):
    """Yield successive n-sized chunks from arr."""
    for i in range(0, len(arr), n):
        yield arr[i: i + n]
        
def get_all_acc_keys(dict_list):
    all_keys = set()

    def recursive_keys(d):
        for k, v in d.items():
            if k.endswith('acc'):
                all_keys.add(k)
            if isinstance(v, dict):
                recursive_keys(v)
                
    for dictionary in dict_list:
        recursive_keys(dictionary)

    return all_keys
    
def summary_metrics(all_metrics):
    if isinstance(all_metrics, dict):
        all_metrics = [all_metrics, ]
    logs_dir = './logs'
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    output_file = os.path.join(logs_dir, 'results.json')
    with open(output_file, 'w', encoding="utf-8") as f:
        json.dump(all_metrics, f, ensure_ascii=False, indent=4)

    mean_metrics = dict()
    for eval in ["pre", "post"]:
        mean_metrics[eval] = dict()
        for key in ["rewrite_acc", "rephrase_acc", 'rewrite_ppl', 'ood_acc']:
            if key in all_metrics[0][eval].keys():
                valid_metrics = [metric[eval][key] for metric in all_metrics if metric[eval][key] is not None]
                if valid_metrics:
                    mean_metrics[eval][key] = np.mean(valid_metrics)
        for key in ["locality", "portability"]:
            if key in all_metrics[0][eval].keys() and all_metrics[0][eval][key] != {}:
                mean_metrics[eval][key] = dict()
                for lkey in get_all_acc_keys(all_metrics):
                    valid_metrics = []
                    for metric in all_metrics:
                        if lkey in metric[eval][key].keys() and metric[eval][key][lkey] is not None:
                            if isinstance(metric[eval][key][lkey], (list, tuple)):
                                if len(metric[eval][key][lkey]) > 0 and metric[eval][key][lkey][0] is not None:
                                    valid_metrics.append(np.mean(metric[eval][key][lkey][0]))
                            else:
                                valid_metrics.append(np.mean(metric[eval][key][lkey]))
                    if len(valid_metrics) > 0:
                        mean_metrics[eval][key][lkey] = np.mean(valid_metrics)
    # mean_metrics["time"] = np.mean([metric["time"] for metric in all_metrics])

    print("Metrics Summary: ", mean_metrics)

def _prepare_requests(prompts: Union[str, List[str]],
                      target_new: Union[str, List[str]],
                      ground_truth: Union[str, List[str]],
                      target_neg: Optional[Union[str, List[str]]] = None,
                      rephrase_prompts: Optional[Union[str, List[str]]] = None,
                      locality_inputs: Optional[Dict] = None,
                      portability_inputs: Optional[Dict] = None,
                      **kwargs
                      ):

    requests = [{
        'prompt': prompt,
        'target_new': target_new_,
        'ground_truth': ground_truth_,
        'portability': {},
        'locality': {}
    }
    for prompt, ground_truth_, target_new_ in zip(prompts, ground_truth, target_new)
    ]

    if target_neg is not None:
        if isinstance(target_neg, str):
            target_neg = [target_neg,]
        assert len(target_neg) == len(prompts)
        for i, request in enumerate(requests):
            request.update(
                {
                    'target_neg': target_neg[i]
                }
            )

    if 'subject' in kwargs:
        if isinstance(kwargs['subject'], str):
            kwargs['subject'] = [kwargs['subject'],]
        else:
            assert len(kwargs['subject']) == len(prompts)
        for prompt_, subject_ in zip(prompts, kwargs['subject']):
            assert subject_ in prompt_, print(f'Subject:{subject_} do not exist in prompt: {prompt_}')

        for i, request in enumerate(requests):
            request.update(
                {
                    'subject': kwargs['subject'][i]
                }
            )
    if 'loc_prompts' in kwargs:
        if isinstance(kwargs['loc_prompts'], str):
            kwargs['loc_prompts'] = [kwargs['loc_prompts'],]
        if len(kwargs['loc_prompts']) < len(requests):
            kwargs['loc_prompts'] = (kwargs['loc_prompts'] * math.ceil(len(requests) / len(kwargs['loc_prompts'])))[:len(requests)]
            random.shuffle(kwargs['loc_prompts'])
        assert len(kwargs['loc_prompts']) == len(prompts)

        for i, request in enumerate(requests):
            request.update(
                {
                    'loc_prompt': kwargs['loc_prompts'][i]
                }
            )

    if rephrase_prompts is not None:
        if isinstance(rephrase_prompts, str):
            rephrase_prompts = [rephrase_prompts,]

        for i, request in enumerate(requests):
            request.update(
                {
                    'rephrase_prompt': rephrase_prompts[i],
                }
            )
    if locality_inputs is not None:
        for locality_key in locality_inputs.keys():
            if isinstance(locality_inputs[locality_key]['prompt'], str):
                locality_inputs[locality_key]['prompt'] = [locality_inputs[locality_key]['prompt'],]
                locality_inputs[locality_key]['ground_truth'] = [locality_inputs[locality_key]['ground_truth'], ]
            assert len(locality_inputs[locality_key]['prompt']) == len(locality_inputs[locality_key]['ground_truth']) \
            == len(requests), print('One Edit instance needs one locality input.....')

            for i, request in enumerate(requests):
                if locality_inputs[locality_key]['prompt'][i] is not None:
                    request['locality'].update(
                        {
                            locality_key: {
                                f'prompt': locality_inputs[locality_key]['prompt'][i],
                                f'ground_truth': locality_inputs[locality_key]['ground_truth'][i]
                            }
                        }
                    )

    if portability_inputs is not None:
        for portability_key in portability_inputs.keys():
            if isinstance(portability_inputs[portability_key]['prompt'], str):
                portability_inputs[portability_key]['prompt'] = [portability_inputs[portability_key]['prompt'],]
                portability_inputs[portability_key]['ground_truth'] = [portability_inputs[portability_key]['ground_truth'], ]
            assert len(portability_inputs[portability_key]['prompt']) == len(portability_inputs[portability_key]['ground_truth']) \
            == len(requests), 'One Edit instance needs one portability input.....'

            for i, request in enumerate(requests):
                if portability_inputs[portability_key]['prompt'][i] is not None:
                    request['portability'].update(
                        {
                            portability_key: {
                                'prompt': portability_inputs[portability_key]['prompt'][i],
                                'ground_truth': portability_inputs[portability_key]['ground_truth'][i]
                            }
                        }
                    )
    return requests
