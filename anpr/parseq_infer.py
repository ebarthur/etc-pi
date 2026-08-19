"""
anpr/parseq_infer.py

Inference-only PARSeq (permuted autoregressive scene text recognition) for
reading the plate crops that anpr/yolov11.py's detector produces.

Why this file exists instead of `import strhub`
-----------------------------------------------
anpr/ocr.ckpt is a full PyTorch Lightning *training* checkpoint from a Kaggle
run (`/kaggle/working/parseq_repo/...`, epoch 43-45, val_accuracy=23.7705) —
it carries optimizer/scheduler/epoch state alongside the weights, and its
`state_dict` keys are prefixed `model.` (the training script wrapped PARSeq in
an outer module). The upstream way to load it is
`PARSeq.load_from_checkpoint()`, which drags in pytorch-lightning, hydra and
the rest of the parseq repo's training scaffolding — none of which a Pi doing
pure inference needs, and all of which is extra install weight and version
risk on an SD card.

So this module rebuilds the same architecture as plain `nn.Module`s and loads
the weights directly. The architecture is not guessed: it is reconstructed
from the `hyper_parameters` block stored *inside the checkpoint itself*, so it
always matches whatever was actually trained rather than a hardcoded default.
Verified against the real checkpoint 2026-08-19: img_size [32,128], patch_size
[4,8], embed_dim 384, enc_depth 12 / enc_num_heads 6, dec_depth 1 /
dec_num_heads 12, max_label_length 25, decode_ar True, refine_iters 1.

The correctness guarantee is `load_state_dict(..., strict=True)` in `_load()`:
it raises on any missing key, unexpected key, or shape mismatch. If this
file's architecture diverged from the trained one in any way, loading fails
loudly at startup rather than silently emitting garbage plate reads.

Only the *inference* path is implemented — the permutation machinery
(perm_num/perm_forward/perm_mirrored) is training-time only; at eval PARSeq
uses a plain left-to-right autoregressive decode plus optional cloze
refinement, which is what `forward()` below does.

Preprocessing uses cv2 + numpy rather than torchvision transforms, to keep
this module's dependency surface to torch + timm + opencv — see `preprocess()`.
"""

import copy
import logging
import math
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn
from timm.models.vision_transformer import VisionTransformer

logger = logging.getLogger(__name__)


class Tokenizer:
    """Charset <-> id mapping, matching parseq's strhub.data.utils.Tokenizer.

    Ordering is load-bearing and must match how the checkpoint was trained:
    EOS first, then the charset, then BOS and PAD last. That makes
    eos_id == 0, bos_id == len(charset) + 1, pad_id == len(charset) + 2.

    Note the two different sizes this implies, both confirmed against the real
    checkpoint tensors:
      - text_embed.embedding has num_tokens = len(charset) + 3 rows (97)
      - head outputs num_tokens - 2 = len(charset) + 1 logits (95) — only the
        charset and EOS are valid *outputs*, never BOS or PAD
    """

    BOS = "[B]"
    EOS = "[E]"
    PAD = "[P]"

    def __init__(self, charset: str) -> None:
        specials_first = (self.EOS,)
        specials_last = (self.BOS, self.PAD)
        self._itos = specials_first + tuple(charset) + specials_last
        self._stoi = {s: i for i, s in enumerate(self._itos)}
        self.eos_id, self.bos_id, self.pad_id = (
            self._stoi[s] for s in specials_first + specials_last
        )

    def __len__(self) -> int:
        return len(self._itos)

    def decode(self, token_dist: torch.Tensor) -> List[Tuple[str, float]]:
        """Greedy-decode a batch of logits into (text, mean_char_confidence).

        Confidence is the mean softmax probability of the chosen tokens up to
        (and excluding) EOS. A read that emits EOS immediately has no
        characters to average over and is reported as ("", 0.0) rather than
        NaN, so callers can treat it as a plain low-confidence miss.
        """
        results = []
        probs = token_dist.softmax(-1)
        conf, ids = probs.max(-1)
        for seq_ids, seq_conf in zip(ids, conf):
            seq_ids = seq_ids.tolist()
            cut = seq_ids.index(self.eos_id) if self.eos_id in seq_ids else len(seq_ids)
            text = "".join(self._itos[i] for i in seq_ids[:cut])
            confidence = float(seq_conf[:cut].mean()) if cut > 0 else 0.0
            results.append((text, confidence))
        return results


class TokenEmbedding(nn.Module):
    """Scaled token embedding. The sqrt(embed_dim) scaling is part of the
    trained weights' expected magnitude, not a free choice."""

    def __init__(self, num_tokens: int, embed_dim: int) -> None:
        super().__init__()
        self.embedding = nn.Embedding(num_tokens, embed_dim)
        self.embed_dim = embed_dim

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        return math.sqrt(self.embed_dim) * self.embedding(tokens)


class Encoder(VisionTransformer):
    """timm ViT with the classifier head removed, exactly as parseq configures it.

    `class_token=False` / `global_pool=''` / `num_classes=0` matter for weight
    compatibility, not just tidiness: with no class token, pos_embed has
    num_patches rows rather than num_patches + 1. Confirmed against the real
    checkpoint — (32/4) * (128/8) = 128 patches, and pos_embed is (1, 128, 384).
    Getting any of these wrong surfaces as a strict-load shape mismatch.
    """

    def __init__(
        self,
        img_size: Sequence[int],
        patch_size: Sequence[int],
        embed_dim: int,
        depth: int,
        num_heads: int,
        mlp_ratio: float,
    ) -> None:
        super().__init__(
            img_size=tuple(img_size),
            patch_size=tuple(patch_size),
            embed_dim=embed_dim,
            depth=depth,
            num_heads=num_heads,
            mlp_ratio=mlp_ratio,
            num_classes=0,
            global_pool="",
            class_token=False,
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # type: ignore[override]
        return self.forward_features(x)


class DecoderLayer(nn.Module):
    """One PARSeq decoder layer: a two-stream (query / content) transformer block.

    The two streams are what makes PARSeq permutation-capable: `query` carries
    positional queries and `content` carries token embeddings, and each is
    attended separately against a shared memory. At inference the content
    stream stops being updated on the final layer (see Decoder.forward) — its
    output would never be read.
    """

    def __init__(self, d_model: int, nhead: int, dim_feedforward: int, dropout: float) -> None:
        super().__init__()
        self.self_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout, batch_first=True)
        self.cross_attn = nn.MultiheadAttention(d_model, nhead, dropout=dropout, batch_first=True)
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)
        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.norm_q = nn.LayerNorm(d_model)
        self.norm_c = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.dropout3 = nn.Dropout(dropout)

    def _stream(
        self,
        tgt: torch.Tensor,
        tgt_norm: torch.Tensor,
        tgt_kv: torch.Tensor,
        memory: torch.Tensor,
        tgt_mask: Optional[torch.Tensor],
        tgt_key_padding_mask: Optional[torch.Tensor],
    ) -> torch.Tensor:
        tgt2, _ = self.self_attn(
            tgt_norm, tgt_kv, tgt_kv, attn_mask=tgt_mask, key_padding_mask=tgt_key_padding_mask
        )
        tgt = tgt + self.dropout1(tgt2)
        tgt2, _ = self.cross_attn(self.norm1(tgt), memory, memory)
        tgt = tgt + self.dropout2(tgt2)
        tgt2 = self.linear2(self.dropout(nn.functional.gelu(self.linear1(self.norm2(tgt)))))
        tgt = tgt + self.dropout3(tgt2)
        return tgt

    def forward(
        self,
        query: torch.Tensor,
        content: torch.Tensor,
        memory: torch.Tensor,
        query_mask: Optional[torch.Tensor] = None,
        content_mask: Optional[torch.Tensor] = None,
        content_key_padding_mask: Optional[torch.Tensor] = None,
        update_content: bool = True,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        query_norm = self.norm_q(query)
        content_norm = self.norm_c(content)
        query = self._stream(
            query, query_norm, content_norm, memory, query_mask, content_key_padding_mask
        )
        if update_content:
            content = self._stream(
                content, content_norm, content_norm, memory, content_mask,
                content_key_padding_mask,
            )
        return query, content


class Decoder(nn.Module):
    def __init__(self, decoder_layer: DecoderLayer, num_layers: int, norm: nn.Module) -> None:
        super().__init__()
        self.layers = nn.ModuleList(copy.deepcopy(decoder_layer) for _ in range(num_layers))
        self.num_layers = num_layers
        self.norm = norm

    def forward(
        self,
        query: torch.Tensor,
        content: torch.Tensor,
        memory: torch.Tensor,
        query_mask: Optional[torch.Tensor] = None,
        content_mask: Optional[torch.Tensor] = None,
        content_key_padding_mask: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        for i, layer in enumerate(self.layers):
            last = i == len(self.layers) - 1
            query, content = layer(
                query, content, memory, query_mask, content_mask,
                content_key_padding_mask, update_content=not last,
            )
        return self.norm(query)


class PARSeq(nn.Module):
    """PARSeq inference module. Mirrors strhub.models.parseq.system.PARSeq's
    eval-time forward pass (AR decode + optional cloze refinement)."""

    def __init__(
        self,
        num_tokens: int,
        max_label_length: int,
        img_size: Sequence[int],
        patch_size: Sequence[int],
        embed_dim: int,
        enc_depth: int,
        enc_num_heads: int,
        enc_mlp_ratio: float,
        dec_depth: int,
        dec_num_heads: int,
        dec_mlp_ratio: float,
        decode_ar: bool,
        refine_iters: int,
        dropout: float = 0.0,
    ) -> None:
        super().__init__()
        self.max_label_length = max_label_length
        self.decode_ar = decode_ar
        self.refine_iters = refine_iters

        self.encoder = Encoder(
            img_size, patch_size, embed_dim=embed_dim, depth=enc_depth,
            num_heads=enc_num_heads, mlp_ratio=enc_mlp_ratio,
        )
        decoder_layer = DecoderLayer(
            embed_dim, dec_num_heads, int(embed_dim * dec_mlp_ratio), dropout
        )
        self.decoder = Decoder(decoder_layer, dec_depth, nn.LayerNorm(embed_dim))
        self.head = nn.Linear(embed_dim, num_tokens - 2)
        self.text_embed = TokenEmbedding(num_tokens, embed_dim)
        self.pos_queries = nn.Parameter(torch.Tensor(1, max_label_length + 1, embed_dim))
        self.dropout = nn.Dropout(p=dropout)

        # Filled in by PARSeqRecognizer._load() from the tokenizer, since the
        # special-token ids depend on the charset length.
        self.bos_id = 0
        self.eos_id = 0
        self.pad_id = 0

    def decode(
        self,
        tgt: torch.Tensor,
        memory: torch.Tensor,
        tgt_mask: Optional[torch.Tensor] = None,
        tgt_padding_mask: Optional[torch.Tensor] = None,
        tgt_query: Optional[torch.Tensor] = None,
        tgt_query_mask: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        N, L = tgt.shape
        # The BOS slot gets no positional query added — it is pure context,
        # which is why it is embedded separately from the rest here.
        null_ctx = self.text_embed(tgt[:, :1])
        tgt_emb = self.pos_queries[:, : L - 1] + self.text_embed(tgt[:, 1:])
        tgt_emb = self.dropout(torch.cat([null_ctx, tgt_emb], dim=1))
        if tgt_query is None:
            tgt_query = self.pos_queries[:, :L].expand(N, -1, -1)
        tgt_query = self.dropout(tgt_query)
        return self.decoder(tgt_query, tgt_emb, memory, tgt_query_mask, tgt_mask, tgt_padding_mask)

    def forward(self, images: torch.Tensor) -> torch.Tensor:
        bs = images.shape[0]
        num_steps = self.max_label_length + 1
        memory = self.encoder(images)
        pos_queries = self.pos_queries[:, :num_steps].expand(bs, -1, -1)
        device = images.device

        # tgt_mask and query_mask deliberately alias the same tensor, matching
        # upstream parseq — the refinement block below mutates it in place and
        # upstream relies on that same aliasing.
        tgt_mask = query_mask = torch.triu(
            torch.full((num_steps, num_steps), float("-inf"), device=device), 1
        )

        if self.decode_ar:
            tgt_in = torch.full((bs, num_steps), self.pad_id, dtype=torch.long, device=device)
            tgt_in[:, 0] = self.bos_id
            logits_steps = []
            for i in range(num_steps):
                j = i + 1
                tgt_out = self.decode(
                    tgt_in[:, :j], memory, tgt_mask[:j, :j],
                    tgt_query=pos_queries[:, i:j], tgt_query_mask=query_mask[i:j, :j],
                )
                p_i = self.head(tgt_out)
                logits_steps.append(p_i)
                if j < num_steps:
                    tgt_in[:, j] = p_i.argmax(-1).squeeze(1)
                    # Every sequence in the batch has emitted EOS — further
                    # steps would only produce padding.
                    if (tgt_in == self.eos_id).any(dim=-1).all():
                        break
            logits = torch.cat(logits_steps, dim=1)
        else:
            tgt_in = torch.full((bs, 1), self.bos_id, dtype=torch.long, device=device)
            logits = self.head(self.decode(tgt_in, memory, tgt_query=pos_queries))

        if self.refine_iters:
            # Cloze refinement: re-decode with the full predicted sequence
            # visible except each position itself, so every character is
            # reconsidered with both left and right context. Clearing the
            # 2nd diagonal turns the causal mask into that cloze mask.
            query_mask[
                torch.triu(torch.ones(num_steps, num_steps, dtype=torch.bool, device=device), 2)
            ] = 0
            bos = torch.full((bs, 1), self.bos_id, dtype=torch.long, device=device)
            for _ in range(self.refine_iters):
                tgt_in = torch.cat([bos, logits[:, :-1].argmax(-1)], dim=1)
                tgt_padding_mask = (tgt_in == self.eos_id).int().cumsum(-1) > 0
                tgt_out = self.decode(
                    tgt_in, memory, tgt_mask, tgt_padding_mask,
                    tgt_query=pos_queries, tgt_query_mask=query_mask[:, : tgt_in.shape[1]],
                )
                logits = self.head(tgt_out)

        return logits


# --- checkpoint loading -----------------------------------------------------
#
# anpr/ocr.ckpt cannot be handed straight to torch.load() on this torch build.
# Its zip container has two non-standard properties (confirmed 2026-08-19):
# explicit directory entries (`data/`, `.data/`), and its records sit at the
# archive root rather than under a single prefix directory. PyTorch infers the
# archive prefix from record 0's name (which is `data/`), then requires every
# record to live under it, so it looks for `data/version`, doesn't find it, and
# fails with 'Expected hasRecord("version") to be true'. The file itself is
# fine — `unzip -t` passes and the md5 is stable across cache drops; only the
# container layout is unusual (it was evidently repacked somewhere between the
# Kaggle run and here).
#
# _normalize_archive() rewrites the container — records copied verbatim under a
# single `ocr/` prefix, which is exactly the layout torch.save() produces (and
# that anpr/crop.pt already has). No tensor data is touched.
#
# Doing that on every startup would mean rewriting 353MB onto the SD card each
# launch, which is exactly the kind of avoidable write this project cares about
# (see plan.md's SD-card-wear notes). So the first successful load also writes a
# compact inference-only sidecar next to the checkpoint — weights plus
# hyper_parameters, without the optimizer/scheduler/loop state that makes up
# most of the 353MB — and later runs load that directly. It is regenerated
# automatically if deleted, and is covered by .gitignore's `anpr/*.pt`.

_INFERENCE_SUFFIX = ".inference.pt"


def _normalize_archive(src: Path, dst: Path) -> None:
    """Repack a torch zip so every record sits under one `ocr/` prefix."""
    import zipfile

    with zipfile.ZipFile(src) as zin:
        names = [i.filename for i in zin.infolist() if not i.is_dir()]
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as zout:
            for name in names:
                zout.writestr(f"ocr/{name}", zin.read(name))


def _load_lightning_checkpoint(path: Path) -> dict:
    """torch.load a Lightning checkpoint, repairing the container if needed.

    weights_only=False: this carries a pickled `hyper_parameters` block, not
    just tensors, and it is a local artifact we trained ourselves rather than
    untrusted input.
    """
    import tempfile

    try:
        return torch.load(str(path), map_location="cpu", weights_only=False)
    except RuntimeError as e:
        if "version" not in str(e) and "subdirectory" not in str(e):
            raise
        # Repair into a scratch file rather than touching the original — the
        # checkpoint is expensive to re-obtain (see the restore-smart-toll skill).
        with tempfile.TemporaryDirectory() as tmp:
            repaired = Path(tmp) / "normalized.ckpt"
            _normalize_archive(path, repaired)
            return torch.load(str(repaired), map_location="cpu", weights_only=False)


class PARSeqRecognizer:
    """Loads anpr/ocr.ckpt and reads text from plate crops.

    Usage:
        ocr = PARSeqRecognizer(ANPR_OCR_CHECKPOINT_PATH)
        text, confidence = ocr.read(bgr_crop)
    """

    def __init__(self, checkpoint_path: str, device: str = "cpu") -> None:
        self.device = torch.device(device)
        self.model, self.tokenizer, self.img_size = self._load(checkpoint_path)
        # _load() builds on CPU (map_location="cpu"); move it onto the target
        # device here so preprocess()'s tensors and the weights agree.
        self.model.to(self.device)

    @staticmethod
    def _load(checkpoint_path: str) -> Tuple[PARSeq, Tokenizer, Tuple[int, int]]:
        path = Path(checkpoint_path)
        if not path.exists():
            raise FileNotFoundError(
                f"PARSeq checkpoint not found at {path}. It is deliberately not in git "
                f"(~353MB) — see .claude/skills/restore-smart-toll/SKILL.md for where to "
                f"restore it from."
            )

        # Fast path: the compact inference-only sidecar written by a previous run.
        compact_path = path.with_suffix(path.suffix + _INFERENCE_SUFFIX)
        if compact_path.exists():
            ckpt = torch.load(str(compact_path), map_location="cpu", weights_only=False)
        else:
            ckpt = _load_lightning_checkpoint(path)

        hp = ckpt.get("hyper_parameters")
        if not hp:
            raise ValueError(
                f"{path} has no hyper_parameters block — cannot reconstruct the "
                f"architecture it was trained with."
            )

        # charset_TRAIN, not charset_test — this sizes the model's head and
        # embedding, so it is not a preference but a hard weight-compatibility
        # requirement. Verified against the real checkpoint: charset_train is 94
        # symbols, giving num_tokens = 97 (matching text_embed.embedding's 97
        # rows) and head outputs = 95 (matching head.weight's 95 rows).
        # charset_test ('0123456789abcdefghijklmnopqrstuvwxyz', 36 symbols) is
        # upstream parseq's *evaluation-time filter* for scoring, not the set the
        # network was built over; using it here would size the head at 37 and
        # fail the strict load below.
        tokenizer = Tokenizer(hp["charset_train"])

        model = PARSeq(
            num_tokens=len(tokenizer),
            max_label_length=hp["max_label_length"],
            img_size=hp["img_size"],
            patch_size=hp["patch_size"],
            embed_dim=hp["embed_dim"],
            enc_depth=hp["enc_depth"],
            enc_num_heads=hp["enc_num_heads"],
            enc_mlp_ratio=hp["enc_mlp_ratio"],
            dec_depth=hp["dec_depth"],
            dec_num_heads=hp["dec_num_heads"],
            dec_mlp_ratio=hp["dec_mlp_ratio"],
            decode_ar=hp["decode_ar"],
            refine_iters=hp["refine_iters"],
            dropout=0.0,  # inference
        )
        model.bos_id, model.eos_id, model.pad_id = (
            tokenizer.bos_id, tokenizer.eos_id, tokenizer.pad_id,
        )

        # The training script wrapped PARSeq one level down, so every key is
        # prefixed `model.` — strip it back off to match this module's layout.
        # Tolerant of both forms, since the compact sidecar stores it stripped.
        state_dict = {
            (k[len("model.") :] if k.startswith("model.") else k): v
            for k, v in ckpt["state_dict"].items()
        }
        # strict=True is the whole correctness argument for this file: any
        # divergence between the architecture above and the trained one shows
        # up here as a missing/unexpected key or a shape mismatch, at load
        # time, instead of as silently wrong plate reads at runtime.
        model.load_state_dict(state_dict, strict=True)
        model.eval()

        # Weights are proven good (strict load passed), so it is safe to cache
        # the compact form for next time. Best-effort: a read-only checkout or a
        # full disk shouldn't stop an otherwise-working model from running.
        if not compact_path.exists():
            try:
                torch.save(
                    {"state_dict": state_dict, "hyper_parameters": hp}, str(compact_path)
                )
                logger.info("Wrote compact inference checkpoint to %s", compact_path)
            except OSError as e:
                logger.warning("Could not write %s: %s", compact_path, e)

        img_size = tuple(hp["img_size"])
        return model, tokenizer, img_size

    def preprocess(self, image: np.ndarray) -> torch.Tensor:
        """BGR uint8 crop -> normalized NCHW float tensor.

        Matches parseq's eval transform (bicubic resize to img_size, scale to
        [0,1], normalize with mean=std=0.5 giving [-1,1]) using cv2 instead of
        torchvision, so this module needs no torchvision import. img_size is
        (height, width); cv2.resize takes (width, height).
        """
        height, width = self.img_size
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_CUBIC)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        normalized = (rgb - 0.5) / 0.5
        tensor = torch.from_numpy(normalized).permute(2, 0, 1).unsqueeze(0)
        return tensor.to(self.device)

    @torch.inference_mode()
    def read(self, image: np.ndarray) -> Tuple[str, float]:
        """Read text from a single plate crop. Returns (text, mean_confidence)."""
        return self.read_batch([image])[0]

    @torch.inference_mode()
    def read_batch(self, images: Sequence[np.ndarray]) -> List[Tuple[str, float]]:
        """Read a batch of crops in one forward pass.

        Worth using over repeated read() calls when a single frame yields
        several plates — the ViT encoder dominates runtime on a Pi CPU and
        batching amortizes it.
        """
        if not images:
            return []
        batch = torch.cat([self.preprocess(img) for img in images], dim=0)
        logits = self.model(batch)
        return self.tokenizer.decode(logits)
