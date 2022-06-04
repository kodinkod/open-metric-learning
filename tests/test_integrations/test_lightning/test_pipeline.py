from functools import partial
from typing import Any, Dict, List

import pytest
import pytorch_lightning as pl
import torch
from torch import nn
from torch.optim import Adam
from torch.utils.data import DataLoader, Dataset

from oml.datasets.triplet import TItem, tri_collate
from oml.lightning.callbacks.metric import MetricValCallback
from oml.losses.triplet import TripletLossPlain, TripletLossWithMiner
from oml.metrics.embeddings import EmbeddingMetrics
from oml.metrics.triplets import AccuracyOnTriplets
from oml.samplers.balanced import SequentialBalanceSampler


class DummyTripletDataset(Dataset):
    def __init__(self, num_triplets: int, im_size: int):
        self.num_triplets = num_triplets
        self.im_size = im_size

    def __getitem__(self, idx: int) -> Dict[str, Any]:
        input_tensors = torch.rand((3, 3, self.im_size, self.im_size))
        return {"input_tensors": input_tensors}

    def __len__(self) -> int:
        return self.num_triplets


class DummyRetrievalDataset(Dataset):
    def __init__(self, labels: List[int], im_size: int):
        self.labels = labels
        self.im_size = im_size

    def __getitem__(self, idx: int) -> Dict[str, Any]:
        input_tensors = torch.rand((3, self.im_size, self.im_size))
        label = torch.tensor(self.labels[idx]).long()
        return {"input_tensors": input_tensors, "labels": label, "is_query": True, "is_gallery": True}

    def __len__(self) -> int:
        return len(self.labels)


class DummyCommonModule(pl.LightningModule):
    def __init__(self, im_size: int):
        super().__init__()
        self.model = nn.Sequential(
            nn.AvgPool2d(kernel_size=(im_size, im_size)), nn.Flatten(start_dim=1), nn.Linear(3, 5), nn.Linear(5, 5)
        )

    def configure_optimizers(self) -> torch.optim.Optimizer:
        return Adam(self.model.parameters(), lr=1e-4)

    def validation_step(self, batch: TItem, batch_idx: int, *dataset_idx: int) -> Dict[str, Any]:
        embeddings = self.model(batch["input_tensors"])
        return {**batch, **{"embeddings": embeddings.detach().cpu()}}


class DummyTripletModule(DummyCommonModule):
    def __init__(self, im_size: int):
        super().__init__(im_size=im_size)
        self.criterion = TripletLossPlain(margin=None)

    def training_step(self, batch_multidataloader: List[TItem], batch_idx: int) -> torch.Tensor:
        embeddings = torch.cat([self.model(batch["input_tensors"]) for batch in batch_multidataloader])
        loss = self.criterion(embeddings)
        return loss


class DummyRetrievalModule(DummyCommonModule):
    def __init__(self, im_size: int):
        super().__init__(im_size=im_size)
        self.criterion = TripletLossWithMiner(margin=None)

    def training_step(self, batch_multidataloader: List[TItem], batch_idx: int) -> torch.Tensor:
        embeddings = torch.cat([self.model(batch["input_tensors"]) for batch in batch_multidataloader])
        labels = torch.cat([batch["labels"] for batch in batch_multidataloader])
        loss = self.criterion(embeddings, labels)
        return loss


def create_triplet_dataloader(num_samples: int, im_size: int) -> DataLoader:
    dataset = DummyTripletDataset(num_triplets=num_samples, im_size=im_size)
    dataloader = DataLoader(dataset=dataset, batch_size=num_samples // 2, num_workers=2, collate_fn=tri_collate)
    return dataloader


def create_retrieval_dataloader(num_samples: int, im_size: int, p: int, k: int) -> DataLoader:
    assert num_samples % (p * k) == 0

    labels = [idx // k for idx in range(num_samples)]

    dataset = DummyRetrievalDataset(labels=labels, im_size=im_size)

    sampler_retrieval = SequentialBalanceSampler(labels=labels, p=p, k=k)
    train_retrieval_loader = DataLoader(
        dataset=dataset,
        sampler=sampler_retrieval,
        num_workers=2,
        batch_size=sampler_retrieval.batch_size,
    )
    return train_retrieval_loader


def create_triplet_callback(loader_idx: int, samples_in_getitem: int) -> MetricValCallback:
    metric = AccuracyOnTriplets(embeddings_key="embeddings")
    metric_callback = MetricValCallback(metric=metric, loader_idx=loader_idx, samples_in_getitem=samples_in_getitem)
    return metric_callback


def create_retrieval_callback(loader_idx: int, samples_in_getitem: int) -> MetricValCallback:
    metric = EmbeddingMetrics(
        embeddings_key="embeddings",
        labels_key="labels",
        is_query_key="is_query",
        is_gallery_key="is_gallery",
        need_map=True,
        need_cmc=True,
        need_precision=True,
    )
    metric_callback = MetricValCallback(metric=metric, loader_idx=loader_idx, samples_in_getitem=samples_in_getitem)
    return metric_callback


@pytest.mark.parametrize(
    "samples_in_getitem, is_error_expected, pipeline",
    [
        (1, True, "triplet"),
        (3, False, "triplet"),
        (5, True, "triplet"),
        (1, False, "retrieval"),
        (2, True, "retrieval"),
    ],
)
@pytest.mark.parametrize("num_dataloaders", [1, 2])
def test_lightning(samples_in_getitem: int, is_error_expected: bool, num_dataloaders: int, pipeline: str) -> None:
    num_samples = 12
    im_size = 6
    p = 2
    k = 3

    if pipeline == "triplet":
        create_dataloader = create_triplet_dataloader
        lightning_module = DummyTripletModule(im_size=im_size)
        create_callback = create_triplet_callback
    elif pipeline == "retrieval":
        create_dataloader = partial(create_retrieval_dataloader, p=p, k=k)
        lightning_module = DummyRetrievalModule(im_size=im_size)
        create_callback = create_retrieval_callback
    else:
        raise ValueError

    train_dataloaders = [create_dataloader(num_samples=num_samples, im_size=im_size) for _ in range(num_dataloaders)]
    val_dataloaders = [create_dataloader(num_samples=num_samples, im_size=im_size) for _ in range(num_dataloaders)]
    callbacks = [create_callback(loader_idx=k, samples_in_getitem=samples_in_getitem) for k in range(num_dataloaders)]

    trainer = pl.Trainer(
        max_epochs=2,
        enable_progress_bar=False,
        num_nodes=1,
        gpus=None,
        replace_sampler_ddp=False,
        callbacks=callbacks,
        num_sanity_val_steps=0,
    )

    if is_error_expected:
        with pytest.raises(ValueError, match=callbacks[0].metric.__class__.__name__):
            trainer.fit(model=lightning_module, train_dataloaders=train_dataloaders, val_dataloaders=val_dataloaders)
            trainer.validate(model=lightning_module, dataloaders=val_dataloaders)
    else:
        trainer.fit(model=lightning_module, train_dataloaders=train_dataloaders, val_dataloaders=val_dataloaders)
        trainer.validate(model=lightning_module, dataloaders=val_dataloaders)