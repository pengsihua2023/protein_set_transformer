from __future__ import annotations

from typing import Optional

import torch
from einops import rearrange, reduce

from pst.nn.utils.distance import pairwise_chamfer_distance
from pst.typing import NO_NEGATIVES_MODES, PairTensor


def positive_sampling(setdist: torch.Tensor) -> torch.Tensor:
    """Perform positive sampling using a set-wise distance metric like Chamfer distance.

    It is recommended that the set-wise distance be calculated BEFORE a model forward
    pass since this will be comparing data in the original dimension compared to the
    embedding dimension. The positive pairs in the embedding dimension will be heavily
    dependent on the model and how it is being trained and how it was initialized.

    Args:
        setdist (torch.Tensor)

    Returns:
        torch.Tensor: tensor with the indices of the positive set
    """
    masked = setdist.clone().fill_diagonal_(torch.inf)
    return masked.argmin(dim=-1)


def _semi_hard_negative_sampling(
    dist_from_pos: torch.Tensor, pos_idx: torch.Tensor, batch_size: Optional[int] = None
) -> torch.Tensor:
    if batch_size is None:
        batch_size = pos_idx.size(0)

    # mask positive idx from being selected -- avoids comparisons with 0
    mask = dist_from_pos.new_full(size=(batch_size, 1), fill_value=torch.inf)
    dist_from_pos = dist_from_pos.scatter(dim=-1, index=pos_idx, src=mask)
    # mask closer than positive idx
    # this is only for when there are actually are negative choices
    # that are greater than the positive idx
    # in other cases when looking for negatives that are closer than the positive
    # just take the absolute value of the dist_from_pos tensor first
    dist_from_pos = torch.where(
        dist_from_pos <= 0.0,
        torch.inf,
        dist_from_pos,
    )
    neg_idx = dist_from_pos.argmin(dim=-1)
    return neg_idx


@torch.no_grad()
def negative_sampling(
    setwise_dist: torch.Tensor,
    X: torch.Tensor,
    Y: Optional[torch.Tensor] = None,
    *,
    pos_idx: Optional[torch.Tensor] = None,
    scale: float = 7.0,
    no_negatives_mode: NO_NEGATIVES_MODES = "closest_to_positive",
) -> tuple[torch.Tensor, torch.Tensor]:
    if Y is None:
        # this chooses the neg sample from the real dataset
        # otherwise, Y is the augmented data, and this will choose from Y
        Y = X
        if pos_idx is None:
            raise ValueError("pos_idx must be supplied if a second tensor Y is not")
    else:
        # pos_idx is just aligned already in the case of sampling from point swapped
        # augmented data
        pos_idx = torch.arange(Y.size(0)).to(Y.device)

    batch_size = pos_idx.size(0)
    pos_idx = rearrange(pos_idx, "batch -> batch 1")

    # in case of sampling from real dataset, diag should be 0 due to self-comparison
    # so don't allow self to be chosen as neg sample
    # for pnt swap, generally diagonal should be the smallest, so chose another
    embed_dist = torch.cdist(X, Y)

    # don't allow points closer than the pos sample be chosen
    # this also has the effect of removing self-comparisons
    pos_dists = embed_dist.gather(dim=-1, index=pos_idx)  # type: ignore

    gt_pos = embed_dist > pos_dists
    has_neg_choices = reduce(gt_pos, "batch1 batch2 -> batch1", "sum").bool()
    dist_from_pos = embed_dist - pos_dists

    # semi-hardish negative mining
    if has_neg_choices.all():
        # neg example is the one that is the next farther after the pos sample
        # dist = 0.0 is pos_idx, dist < 0.0 is closer than pos,
        # but we want just farther than pos
        neg_idx = _semi_hard_negative_sampling(
            dist_from_pos=dist_from_pos, pos_idx=pos_idx, batch_size=batch_size
        )
    else:
        neg_idx = pos_idx.new_full((batch_size,), fill_value=-1)
        has_neg_idx = has_neg_choices.nonzero(as_tuple=True)[0]
        no_neg_idx = has_neg_choices.logical_not().nonzero(as_tuple=True)[0]

        # calc negative for those with proper negative ie negative
        # is just the next set farther than the positive
        has_neg_dist_from_pos = dist_from_pos[has_neg_idx]
        has_neg_neg_idx = _semi_hard_negative_sampling(
            dist_from_pos=has_neg_dist_from_pos,
            pos_idx=pos_idx[has_neg_idx],
            batch_size=None,
        )
        neg_idx[has_neg_idx] = has_neg_neg_idx

        if no_negatives_mode == "closest_to_positive":
            no_neg_dist_from_pos = dist_from_pos[no_neg_idx].abs()
            no_neg_neg_idx = _semi_hard_negative_sampling(
                dist_from_pos=no_neg_dist_from_pos,
                pos_idx=pos_idx[no_neg_idx],
                batch_size=None,
            )
        elif no_negatives_mode == "closest_to_anchor":
            dist_from_anchor = embed_dist[no_neg_idx]
            # mask anchor
            dist_from_anchor[:, no_neg_idx] = torch.inf
            no_neg_neg_idx = dist_from_anchor.argmin(dim=-1)
        else:
            raise ValueError(
                f"Invalid strategy passed for no negatives: {no_negatives_mode=}"
            )
        neg_idx[no_neg_idx] = no_neg_neg_idx

    # calc negative sample weight
    neg_setwise_dists = setwise_dist.gather(
        dim=-1, index=neg_idx.unsqueeze(1)
    ).squeeze()
    setwise_std = setwise_dist.std()
    denom = 2 * (scale * setwise_std) ** 2
    neg_weight = torch.exp(-neg_setwise_dists / denom)

    return neg_idx, neg_weight


def point_swap_sampling(
    batch: torch.Tensor,
    pos_idx: torch.Tensor,
    item_flow: torch.Tensor,
    sizes: torch.Tensor,
    sample_rate: float,
) -> PairTensor:
    """Perform point swap sampling data augmentation.

    Args:
        batch (torch.Tensor): stacked batch, shape: [total_items, feat_dim]
        ptr (torch.Tensor): data batch ptr to start/stop for each set in
            batch, shape: [batch_size + 1]
        pos_idx (torch.Tensor): at position i, holds the closest set j
            index to set i, shape: [batch_size]
        item_flow (FlowType): dict mapping pairs of indices (i,j) to the
            closest item index for each item m in set i to the items n
            in set j
        sizes (torch.Tensor): shape: [batch_size], size of each point set
        sample_rate (float): rate of point swapping, (0.0, 1.0)

    Returns:
        PairTensor: augmented batch and the indices of the nodes that were
            swapped around
    """
    if sample_rate <= 0.0 or sample_rate >= 1.0:
        raise ValueError(f"Provided {sample_rate=} must be in the range (0.0, 1.0)")

    pos_idx = pos_idx.repeat_interleave(sizes).unsqueeze(1)
    aug_idx = item_flow.gather(dim=1, index=pos_idx).squeeze()

    # sample from uniform [0, 1) distribution to determine whether to swap
    # True = should swap, False = should not swap
    # TODO: this is technically the opposite of the sample rate
    # if we want to swap 0.4 of proteins,
    # then the number of proteins swapped here is 0.6, ie 1 - 0.4
    mask = torch.rand(aug_idx.size(0), device=aug_idx.device) < sample_rate
    default = torch.arange(aug_idx.size(0), device=aug_idx.device)
    aug_idx = torch.where(mask, aug_idx, default)

    augmented_batch = batch[aug_idx]
    return augmented_batch, aug_idx


def heuristic_augmented_negative_sampling(
    X_anchor: torch.Tensor,
    X_aug: torch.Tensor,
    y_aug: torch.Tensor,
    neg_idx: torch.Tensor,
    ptr: torch.Tensor,
    scale: float = 7.0,
) -> tuple[torch.Tensor, torch.Tensor]:
    chamfer_distances: list[float] = list()
    for i, j in enumerate(neg_idx):
        j = int(j)

        start_i = ptr[i]
        stop_i = ptr[i + 1]
        start_j = ptr[j]
        stop_j = ptr[j + 1]

        set_i = X_anchor[start_i:stop_i]
        set_j = X_aug[start_j:stop_j]

        chamfer_dist = pairwise_chamfer_distance(set_i, set_j)
        chamfer_distances.append(chamfer_dist)

    aug_neg_dist = torch.tensor(chamfer_distances, device=X_anchor.device)
    ### AUG NEG WEIGHTS ###
    # TODO: using std here may make the weights really small since there are fewer
    # distances gonna use mean for now
    std = aug_neg_dist.std()
    denom = 2 * (scale * std) ** 2
    aug_neg_weights = torch.exp(-aug_neg_dist / denom)

    return y_aug[neg_idx], aug_neg_weights
