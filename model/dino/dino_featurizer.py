import torch
import torch.nn as nn
import model.dino.vision_transformer as vits  # noqa

__all__ = ["DinoFeaturizer"]


class DinoFeaturizer(nn.Module):

    def __init__(self, cfg: dict):  # cfg["pretrained"]
        super().__init__()
        self.cfg = cfg

        arch = self.cfg["model_type"]  # vit_small, vit_base
        patch_size = self.cfg["dino_patch_size"]
        self.patch_size = patch_size

        self.freeze_backbone: bool = cfg.get("freeze_backbone", True)
        self.backbone = vits.__dict__[arch](patch_size=patch_size, num_classes=0)
        self.backbone.requires_grad_(not self.freeze_backbone)
        self.backbone.eval()

        drop_prob = cfg.get("drop_prob", 0.1)
        # self.is_dropout = cfg["dropout"]
        # self.dropout = torch.nn.Dropout2d(p=drop_prob)

        if arch == "vit_small" and patch_size == 16:
            url = "dino_deitsmall16_pretrain/dino_deitsmall16_pretrain.pth"
        elif arch == "vit_small" and patch_size == 8:
            url = "dino_deitsmall8_300ep_pretrain/dino_deitsmall8_300ep_pretrain.pth"
        elif arch == "vit_base" and patch_size == 16:
            url = "dino_vitbase16_pretrain/dino_vitbase16_pretrain.pth"
        elif arch == "vit_base" and patch_size == 8:
            url = "dino_vitbase8_pretrain/dino_vitbase8_pretrain.pth"
        else:
            raise ValueError(f"Unknown arch {arch} and patch size {patch_size}.")

        if cfg["pretrained_weights"] is not None:
            state_dict = torch.load(cfg["pretrained_weights"], map_location="cpu")
            state_dict = state_dict["teacher"]
            # remove `module.` prefix
            state_dict = {k.replace("module.", ""): v for k, v in state_dict.items()}
            # remove `backbone.` prefix induced by multicrop wrapper
            state_dict = {k.replace("backbone.", ""): v for k, v in state_dict.items()}

            msg = self.backbone.load_state_dict(state_dict, strict=False)
            print(f'Pretrained weights found at {cfg["pretrained_weights"]} and loaded with msg: {msg}')
        else:
            print("Since no pretrained weights have been provided, we load the reference pretrained DINO weights.")
            state_dict = torch.hub.load_state_dict_from_url(url="https://dl.fbaipublicfiles.com/dino/" + url)
            self.backbone.load_state_dict(state_dict, strict=True)

        if arch == "vit_small":
            self.n_feats = 384
        elif arch == "vit_base":
            self.n_feats = 768

    def train(self, mode: bool = True):
        super().train(mode=mode)
        if self.freeze_backbone:
            self.backbone.eval()
        return self

    def forward(self, img: torch.Tensor) -> torch.Tensor:
        """DINO forward
        :param img:     (batch_size, 3, 224, 224)
        :return:        (batch_size, 384, 28, 28)
        """
        b, c, h, w = img.shape
        assert (h % self.patch_size == 0) and (w % self.patch_size == 0)
        feat_h, feat_w = h // self.patch_size, w // self.patch_size

        self.backbone.eval()
        if self.freeze_backbone:
            with torch.no_grad():
                feat, _, _ = self.backbone.get_intermediate_feat(img, n=1)
        else:
            feat, _, _ = self.backbone.get_intermediate_feat(img, n=1)

        feat = feat[0]  # (b, 1+28x28, 384)
        feat = feat[:, 1:, :].reshape(b, feat_h, feat_w, -1).permute(0, 3, 1, 2).contiguous()  # (b, 384, 28, 28)

        return feat

