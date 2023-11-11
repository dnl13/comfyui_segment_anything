import os
import sys
sys.path.append(
    os.path.dirname(os.path.abspath(__file__))
)

import copy
import torch
from torchvision.transforms import ToTensor
import numpy as np
from PIL import Image
import logging
from torch.hub import download_url_to_file
from urllib.parse import urlparse
import folder_paths
import comfy.model_management
from sam_hq.predictor import SamPredictorHQ
from sam_hq.build_sam_hq import sam_model_registry
from local_groundingdino.datasets import transforms as T
from local_groundingdino.util.utils import clean_state_dict as local_groundingdino_clean_state_dict
from local_groundingdino.util.slconfig import SLConfig as local_groundingdino_SLConfig
from local_groundingdino.models import build_model as local_groundingdino_build_model

logger = logging.getLogger('comfyui_segment_anything')
to_tensor = ToTensor()

sam_model_dir = os.path.join(folder_paths.models_dir, "sams")
sam_model_list = {
    "sam_vit_h (2.56GB)": {
        "model_url": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth"
    },
    "sam_vit_l (1.25GB)": {
        "model_url": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_l_0b3195.pth"
    },
    "sam_vit_b (375MB)": {
        "model_url": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth"
    },
    "sam_hq_vit_h (2.57GB)": {
        "model_url": "https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_h.pth"
    },
    "sam_hq_vit_l (1.25GB)": {
        "model_url": "https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_l.pth"
    },
    "sam_hq_vit_b (379MB)": {
        "model_url": "https://huggingface.co/lkeab/hq-sam/resolve/main/sam_hq_vit_b.pth"
    },
    "mobile_sam(39MB)": {
        "model_url": "https://github.com/ChaoningZhang/MobileSAM/blob/master/weights/mobile_sam.pt"
    }
}

groundingdino_model_dir = os.path.join(
    folder_paths.models_dir, "grounding-dino")
groundingdino_model_list = {
    "GroundingDINO_SwinT_OGC (694MB)": {
        "config_url": "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/GroundingDINO_SwinT_OGC.cfg.py",
        "model_url": "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/groundingdino_swint_ogc.pth",
    },
    "GroundingDINO_SwinB (938MB)": {
        "config_url": "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/GroundingDINO_SwinB.cfg.py",
        "model_url": "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/groundingdino_swinb_cogcoor.pth"
    },
}


def list_files(dirpath, extensions=[]):
    return [f for f in os.listdir(dirpath) if os.path.isfile(os.path.join(dirpath, f)) and f.split('.')[-1] in extensions]


def list_sam_model():
    return list(sam_model_list.keys())


def load_sam_model(model_name , use_cpu=False):
    device = comfy.model_management.get_torch_device() if use_cpu == False else torch.device("cpu")
    print(f"\033[1;32mload_sam_model using:\033[0m {device}")
    sam_checkpoint_path = get_local_filepath(
        sam_model_list[model_name]["model_url"], sam_model_dir)
    model_file_name = os.path.basename(sam_checkpoint_path)
    model_type = model_file_name.split('.')[0]
    if 'hq' not in model_type and 'mobile' not in model_type:
        model_type = '_'.join(model_type.split('_')[:-1])
    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint_path)
    sam.to(device=device)
    sam.eval()
    sam.model_name = model_file_name
    return sam


def get_local_filepath(url, dirname, local_file_name=None):
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    if not local_file_name:
        parsed_url = urlparse(url)
        local_file_name = os.path.basename(parsed_url.path)
    destination = os.path.join(dirname, local_file_name)
    if not os.path.exists(destination):
        logging.warn(f'downloading {url} to {destination}')
        download_url_to_file(url, destination)
    return destination


def load_groundingdino_model(model_name, use_cpu=False):
    device = comfy.model_management.get_torch_device() if use_cpu == False else torch.device("cpu")
    print(f"\033[1;32mload_groundingdino_model using:\033[0m {device}")
    dino_model_args = local_groundingdino_SLConfig.fromfile(
        get_local_filepath(
            groundingdino_model_list[model_name]["config_url"],
            groundingdino_model_dir
        ),

    )
    dino = local_groundingdino_build_model(dino_model_args)
    checkpoint = torch.load(
        get_local_filepath(
            groundingdino_model_list[model_name]["model_url"],
            groundingdino_model_dir,
        ),
    )
    dino.load_state_dict(local_groundingdino_clean_state_dict(
        checkpoint['model']), strict=False)
    dino.to(device=device)
    dino.eval()
    return dino


def list_groundingdino_model():
    return list(groundingdino_model_list.keys())


def groundingdino_predict(
    dino_model,
    image,
    prompt,
    box_threshold,
    use_cpu
):
    device = comfy.model_management.get_torch_device() if use_cpu == False else torch.device("cpu")
    print(f"\033[1;32mgroundingdino_predict using:\033[0m {device}")
    def load_dino_image(image_pil):
        transform = T.Compose(
            [
                T.RandomResize([800], max_size=1333),
                T.ToTensor(),
                T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
            ]
        )
        image, _ = transform(image_pil, None)  # 3, h, w
        return image

    def get_grounding_output(model, image, caption, box_threshold, device):
        print(f"\033[1;32mget_grounding_output using:\033[0m {device}")
        caption = caption.lower()
        caption = caption.strip()
        if not caption.endswith("."):
            caption = caption + "."
        image = image.to(device)
        with torch.no_grad():
            outputs = model(image[None], captions=[caption])
        logits = outputs["pred_logits"].sigmoid()[0]  # (nq, 256)
        boxes = outputs["pred_boxes"][0]  # (nq, 4)
        # filter output
        logits_filt = logits.clone()
        boxes_filt = boxes.clone()
        filt_mask = logits_filt.max(dim=1)[0] > box_threshold
        logits_filt = logits_filt[filt_mask]  # num_filt, 256
        boxes_filt = boxes_filt[filt_mask]  # num_filt, 4
        return boxes_filt.to(device)

    dino_image = load_dino_image(image.convert("RGB"))
    boxes_filt = get_grounding_output(
        dino_model, dino_image, prompt, box_threshold, device
    )
    H, W = image.size[1], image.size[0]
    for i in range(boxes_filt.size(0)):
        boxes_filt[i] = boxes_filt[i] * torch.Tensor([W, H, W, H]).to(device)
        boxes_filt[i][:2] -= boxes_filt[i][2:] / 2
        boxes_filt[i][2:] += boxes_filt[i][:2]
    return boxes_filt.to(device)


def create_pil_output(image_np, masks, boxes_filt):
    output_masks, output_images = [], []
    boxes_filt = boxes_filt.numpy().astype(int) if boxes_filt is not None else None
    for mask in masks:
        output_masks.append(Image.fromarray(np.any(mask, axis=0)))
        image_np_copy = copy.deepcopy(image_np)
        image_np_copy[~np.any(mask, axis=0)] = np.array([0, 0, 0, 0])
        output_images.append(Image.fromarray(image_np_copy))
    return output_images, output_masks


def create_tensor_output(image_np, masks, boxes_filt, device):
    output_masks, output_images = [], []
    boxes_filt = boxes_filt.cpu().numpy().astype(int) if boxes_filt is not None else None
    for mask in masks:
        image_np_copy = copy.deepcopy(image_np)
        image_np_copy[~np.any(mask, axis=0)] = np.array([0, 0, 0, 0])
        output_image, output_mask = split_image_mask(
            Image.fromarray(image_np_copy), device)
        output_masks.append(output_mask)
        output_images.append(output_image)
    return (output_images, output_masks)


def split_image_mask(image, device):
    image_rgb = image.convert("RGB")
    image_rgb = np.array(image_rgb).astype(np.float32) / 255.0
    image_rgb = torch.from_numpy(image_rgb)[None,]
    if 'A' in image.getbands():
        mask = np.array(image.getchannel('A')).astype(np.float32) / 255.0
        mask = torch.from_numpy(mask)[None,]
    else:
        mask = torch.zeros((64, 64), dtype=torch.float32, device=device)
    return (image_rgb, mask)


def sam_segment(
    sam_model,
    image,
    boxes,
    multimask,
    use_cpu
):  
    device = comfy.model_management.get_torch_device() if use_cpu == False else torch.device("cpu")
    print(f"\033[1;32msam_segment using:\033[0m {device}")
    if boxes.shape[0] == 0:
        return None
    sam_is_hq = False
    # TODO: more elegant
    if hasattr(sam_model, 'model_name') and 'hq' in sam_model.model_name:
        sam_is_hq = True
    predictor = SamPredictorHQ(sam_model, sam_is_hq)
    image_np = np.array(image)
    image_np_rgb = image_np[..., :3]
    predictor.set_image(image_np_rgb)
    transformed_boxes = predictor.transform.apply_boxes_torch(
        boxes, image_np.shape[:2])
    masks, _, _ = predictor.predict_torch(
        point_coords=None,
        point_labels=None,
        boxes=transformed_boxes.to(device),
        multimask_output=False)
    
    if multimask is not False:
        print(f"\033[1;32msam_segment using multimask:\033[0m {multimask}")

        output_images, output_masks = [], []
        for batch_index in range(masks.size(0)):
            mask_np =  masks[batch_index].permute( 1, 2, 0).cpu().numpy()# H.W.C
            image_with_alpha = Image.fromarray(np.concatenate((image_np_rgb, mask_np * 255), axis=2).astype(np.uint8), 'RGBA')
            _, msk = split_image_mask(image_with_alpha, device)
            r, g, b, a = image_with_alpha.split()

            black_image = Image.new("RGB", image.size, (0, 0, 0))
            black_image.paste(image_with_alpha, mask=image_with_alpha.split()[3])

            rgb_ts = to_tensor(black_image)
            rgb_ts = rgb_ts.unsqueeze(0)
            rgb_ts = rgb_ts.permute(0, 2, 3, 1)

            output_images.append(rgb_ts)
            output_masks.append(msk)
                        
        return (output_images, output_masks)
    else:
        masks = masks.permute(1, 0, 2, 3).cpu().numpy()
        return create_tensor_output(image_np, masks, boxes, device)


class SAMModelLoader:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model_name": (list_sam_model(), ),
            },
            "optional":{
                "use_cpu":{"USE_CPU":{"default":False}}
            }
        }
    CATEGORY = "segment_anything"
    FUNCTION = "main"
    RETURN_TYPES = ("SAM_MODEL", )

    def main(self, model_name, use_cpu=False):
        sam_model = load_sam_model(model_name, use_cpu)
        return (sam_model, )


class GroundingDinoModelLoader:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model_name": (list_groundingdino_model(), ),
            },
            "optional":{
                "use_cpu":{"USE_CPU":{"default":False}}
            }
        }
    CATEGORY = "segment_anything"
    FUNCTION = "main"
    RETURN_TYPES = ("GROUNDING_DINO_MODEL", )

    def main(self, model_name, use_cpu=False):
        dino_model = load_groundingdino_model(model_name,use_cpu)
        return (dino_model, )


class GroundingDinoSAMSegment:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "sam_model": ('SAM_MODEL', {}),
                "grounding_dino_model": ('GROUNDING_DINO_MODEL', {}),
                "image": ('IMAGE', {}),
                "prompt": ("STRING", {"default": "arms, legs, eyes, hair, head","multiline": True}),
                "box_threshold": ("FLOAT", {
                    "default": 0.3,
                    "min": 0,
                    "max": 1.0,
                    "step": 0.01
                }),
                "multimask": ('BOOLEAN', {"default":False}),
            },
            "optional":{
                "use_cpu":{"USE_CPU":{"default":False}}
            }
        }
    CATEGORY = "segment_anything"
    FUNCTION = "main"
    RETURN_TYPES = ("IMAGE", "MASK")

    def main(self, grounding_dino_model, sam_model, image, prompt, box_threshold,multimask=False, use_cpu=False):
        res_images = []
        res_masks = []
        for item in image:
            item = Image.fromarray(
                np.clip(255. * item.cpu().numpy(), 0, 255).astype(np.uint8)).convert('RGBA')
            boxes = groundingdino_predict(
                grounding_dino_model,
                item,
                prompt,
                box_threshold,
                use_cpu
            )
            (images, masks) = sam_segment(
                sam_model,
                item,
                boxes,
                multimask,
                use_cpu
            )
            res_images.extend(images)
            res_masks.extend(masks)
        res_images = torch.cat(res_images, dim=0)
        res_masks = torch.cat(res_masks, dim=0)
        return (res_images, res_masks, )

class DeviceSelector:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "use_cpu": ('BOOLEAN', {"default":False}),
            }
        }
    CATEGORY = "segment_anything"
    FUNCTION = "main"
    RETURN_TYPES = ("USE_CPU",)

    def main(self, use_cpu):
        return (use_cpu,)

class BatchSelector:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ('IMAGE', {}),
                "mask": ('MASK', {}),
                "batch_select": ("FLOAT", {
                    "default": 1,
                    "min": 1,
                    "max": 1000000,
                    "step": 1
                }),
            }
        }
    CATEGORY = "segment_anything"
    FUNCTION = "main"
    RETURN_TYPES = ("IMAGE","MASK",)

    def main(self, image, mask, batch_select):
        selector = round(batch_select-1)
        selected_image = image[selector]
        selected_image = selected_image.unsqueeze(0)
        selected_mask = mask[selector]
        return (selected_image, selected_mask, )
    

"""
if __name__ == "__main__":
    input_image = Image.open(
        '/data/dev/comfyui-latest/custom_nodes/comfyui_segment_anything/human.jpg').convert('RGBA')
    dino_model = load_groundingdino_model('GroundingDINO_SwinT_OGC (694MB)')
    boxes = groundingdino_predict(
        dino_model,
        input_image,
        'face . glasses . forehead',
        0.3
    )
    sam_model = load_sam_model('sam_hq_vit_h (2.57GB)')
    (output_images, output_masks) = sam_segment(
        sam_model,
        input_image,
        boxes
    )
    for i in range(len(output_images)):
        output_images[i].save(f"result_{i}.png")
"""