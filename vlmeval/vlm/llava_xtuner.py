import os
import os.path as osp
import warnings

import pandas as pd
import torch
from huggingface_hub import snapshot_download
from PIL import Image
from transformers import (AutoModel, AutoModelForCausalLM, AutoTokenizer,
                          CLIPImageProcessor, CLIPVisionModel,
                          GenerationConfig)

from ..smp import cn_string, decode_base64_to_image_file, get_cache_path, read_ok
from ..utils import DATASET_TYPE


class LLaVA_XTuner:

    INSTALL_REQ = True

    def __init__(self,
                 llava_path,
                 llm_path=None,
                 visual_encoder_path=None,
                 visual_select_layer=-2,
                 prompt_template=None,
                 torch_dtype=torch.float16,
                 **kwargs):
        try:
            from peft import PeftModel
            from xtuner.tools.utils import get_chat_utils
            from xtuner.utils import PROMPT_TEMPLATE
        except Exception:
            warnings.warn(
                'Please install xtuner with `pip install -U xtuner` before '
                'using LLaVA_XTuner')
            exit(-1)

        if not osp.isdir(llava_path):
            cache_path = get_cache_path(llava_path)
            if cache_path is not None:
                llava_path = cache_path
            else:
                llava_path = snapshot_download(repo_id=llava_path)
        assert osp.exists(llava_path) and osp.isdir(llava_path)

        # build visual_encoder
        if 'llm' in os.listdir(llava_path):
            assert llm_path is None, (
                "Please don't specify the `llm_path` since passed "
                '`llava_path` contains a LLM!')
            llm_path = osp.join(llava_path, 'llm')
        else:
            assert llm_path is not None, 'Please specify the `llm_path`!'

        llm = AutoModelForCausalLM.from_pretrained(llm_path,
                                                   trust_remote_code=True,
                                                   torch_dtype=torch_dtype,
                                                   device_map='cpu')
        tokenizer = AutoTokenizer.from_pretrained(llm_path,
                                                  trust_remote_code=True,
                                                  encode_special_tokens=True)
        print(f'Load LLM from {llm_path}')

        # build visual_encoder
        if 'visual_encoder' in os.listdir(llava_path):
            assert visual_encoder_path is None, (
                "Please don't specify the `visual_encoder_path` since passed "
                '`llava_path` contains a visual encoder!')
            visual_encoder_path = osp.join(llava_path, 'visual_encoder')
        else:
            assert visual_encoder_path is not None, (
                'Please specify the `visual_encoder_path`!')
        visual_encoder = CLIPVisionModel.from_pretrained(
            visual_encoder_path, torch_dtype=torch_dtype, device_map='cpu')
        image_processor = CLIPImageProcessor.from_pretrained(
            visual_encoder_path)
        print(f'Load visual_encoder from {visual_encoder_path}')

        # load adapter
        if 'llm_adapter' in os.listdir(llava_path):
            adapter_path = osp.join(llava_path, 'llm_adapter')
            llm = PeftModel.from_pretrained(llm, adapter_path, device_map='cpu')
            print(f'Load LLM adapter from {llava_path}')
        if 'visual_encoder_adapter' in os.listdir(llava_path):
            adapter_path = osp.join(llava_path, 'visual_encoder_adapter')
            visual_encoder = PeftModel.from_pretrained(visual_encoder, adapter_path, device_map='cpu')
            print(f'Load visual_encoder adapter from {llava_path}')

        # build projector
        projector_path = osp.join(llava_path, 'projector')
        projector = AutoModel.from_pretrained(projector_path, torch_dtype=torch_dtype, device_map='cpu')
        print(f'Load projector from {llava_path}')

        llm.eval()
        visual_encoder.eval()
        projector.eval()

        self.llm = llm.cuda()
        self.tokenizer = tokenizer
        self.visual_encoder = visual_encoder.cuda()
        self.image_processor = image_processor
        self.projector = projector.cuda()
        self.visual_select_layer = visual_select_layer
        if prompt_template is not None:
            self.prompt_template = PROMPT_TEMPLATE[prompt_template]
        else:
            self.prompt_template = None

        _, self.stop_criteria = get_chat_utils(self.llm)

        kwargs_default = dict(max_new_tokens=100,
                              do_sample=False,
                              eos_token_id=self.tokenizer.eos_token_id,
                              pad_token_id=self.tokenizer.pad_token_id
                              if self.tokenizer.pad_token_id is not None else
                              self.tokenizer.eos_token_id)
        if len(kwargs) > 0:
            kwargs_default.update(kwargs)
            warnings.warn(f'Following kwargs received: {kwargs}, '
                          'will use as generation config.')
        self.gen_config = GenerationConfig(**kwargs_default)

    def build_prompt(self, line, dataset=None):
        from ..utils import img_root_map
        assert dataset is None or isinstance(dataset, str)
        img_root = osp.join('images', img_root_map[dataset])
        os.makedirs(img_root, exist_ok=True)

        if isinstance(line['image'], list):
            tgt_path = []
            for img, im_name in zip(line['image'], line['image_path']):
                path = osp.join(img_root, im_name)
                if not read_ok(path):
                    decode_base64_to_image_file(img, path)
                tgt_path.append(path)
        else:
            tgt_path = osp.join(img_root, f"{line['index']}.jpg")
            if not read_ok(tgt_path):
                decode_base64_to_image_file(line['image'], tgt_path)

        if dataset is not None and DATASET_TYPE(dataset) == 'multi-choice':
            question = line['question']
            hint = line['hint'] if ('hint' in line
                                    and not pd.isna(line['hint'])) else None
            if hint is not None:
                question = hint + ' ' + question

            option_candidate = ['A', 'B', 'C', 'D', 'E']
            options = {
                cand: line[cand]
                for cand in option_candidate
                if cand in line and not pd.isna(line[cand])
            }
            options_prompt = 'There are several options:\n'
            for key, item in options.items():
                options_prompt += f'{key}. {item}\n'
            prompt = question + ' ' + options_prompt

            if not cn_string(prompt):
                prompt = prompt + '\n' + ("Answer with the option's letter "
                                          'from the given choices directly.')
            else:
                prompt = prompt + '\n' + '请直接回答选项字母。'
        else:
            prompt = line['question']

        return {'image': tgt_path, 'text': prompt}

    def generate(self, image_path, prompt, dataset=None):
        from xtuner.dataset.utils import expand2square
        from xtuner.model.utils import prepare_inputs_labels_for_multimodal
        from xtuner.utils import DEFAULT_IMAGE_TOKEN, IMAGE_TOKEN_INDEX
        image = Image.open(image_path).convert('RGB')
        image = expand2square(
            image,
            tuple(int(x * 255) for x in self.image_processor.image_mean))
        image = self.image_processor.preprocess(
            image, return_tensors='pt')['pixel_values'][0]
        image = image.cuda().unsqueeze(0)
        visual_outputs = self.visual_encoder(image, output_hidden_states=True)
        pixel_values = self.projector(
            visual_outputs.hidden_states[self.visual_select_layer][:, 1:])

        inputs = DEFAULT_IMAGE_TOKEN + '\n' + prompt

        if self.prompt_template:
            inputs = self.prompt_template['INSTRUCTION'].format(input=inputs)

        chunk_encode = []
        for idx, chunk in enumerate(inputs.split(DEFAULT_IMAGE_TOKEN)):
            if idx == 0:
                cur_encode = self.tokenizer(chunk)
            else:
                cur_encode = self.tokenizer(chunk, add_special_tokens=False)
            chunk_encode.append(cur_encode)
        assert len(chunk_encode) == 2
        ids = []
        for idx, cur_chunk_encode in enumerate(chunk_encode):
            ids.extend(cur_chunk_encode['input_ids'])
            if idx != len(chunk_encode) - 1:
                ids.append(IMAGE_TOKEN_INDEX)
        ids = torch.tensor(ids).cuda().unsqueeze(0)
        mm_inputs = prepare_inputs_labels_for_multimodal(
            llm=self.llm, input_ids=ids, pixel_values=pixel_values)

        generate_output = self.llm.generate(
            **mm_inputs,
            generation_config=self.gen_config,
            streamer=None,
            bos_token_id=self.tokenizer.bos_token_id,
            stopping_criteria=self.stop_criteria)
        predict = self.tokenizer.decode(generate_output[0],
                                        skip_special_tokens=True).strip()
        return predict
