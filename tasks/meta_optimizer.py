import random
from abc import ABC, abstractmethod
from typing import Iterable, Literal

import torch
from beartype import beartype
from lightning import LightningModule
from torch import Tensor

from models.context_aggregator import ContextAggregator
from models.implicit import ImplicitModel
from models.predictor import Predictor
from utils import torch_pca
import wandb
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt


class MetaOptimizerExplicit(ABC, LightningModule):
    MetaObjective = Literal["train", "prequential"]

    @beartype
    def __init__(
        self,
        meta_objective: MetaObjective,
        context_aggregator: ContextAggregator,
        predictor: Predictor,
        min_train_samples: int = 1,
        lr: float = 1e-4,
        log_eff_zdim: bool = False,
    ):
        super().__init__()
        self.save_hyperparameters(ignore=["context_aggregator", "predictor"])

        self.meta_objective = meta_objective
        self.context_aggregator = context_aggregator
        self.predictor = predictor

    @beartype
    def forward(
        self, x: dict[str, Tensor]
    ) -> tuple[
        dict[str, Tensor],
        dict[str, Tensor],
        dict[str, Tensor],
        dict[str, Tensor],
        dict[str, Tensor],
    ]:
        """Return the training predictions, next-token predictions, and aggregated context z.

        Args:
            x (dict[str, Tensor]):  Input data, each with shape (samples, tasks, *).

        Returns:
            tuple[dict[str, Tensor], dict[str, Tensor]]: Training predictions, next-token predictions, and aggregated context z.
            - training predictions: (samples - min_train_samples, tasks, *).
            - next-token predictions: (samples - min_train_samples, tasks, *).
            - x_train: (samples - min_train_samples, tasks, *).
            - x_nexttoken: (samples - min_train_samples, tasks, *).
            - z: (samples - min_train_samples, tasks, *).
        """
        z = self.context_aggregator.forward(x)

        # z goes from 0 context to full context.
        # We index this way because for the "prequential" optimizer the final z never gets a training signal,
        # and for the "train" optimizer the first z never gets a training signal.
        z = {name: z[name][self.hparams.min_train_samples : -1] for name in z}

        # x_train is some random location previously in the context,
        # and x_nexttoken is the next token.
        train_indices = [
            random.randint(0, i)
            for i in range(
                self.hparams.min_train_samples - 1,
                len(x[list(x.keys())[0]]) - 1,
            )
        ]
        x_train = {name: x[name][train_indices] for name in x}
        x_nexttoken = {name: x[name][self.hparams.min_train_samples :] for name in x}

        mode = self.training
        if self.meta_objective == "train":
            preds_train = self.predictor.forward(x_train, z)
            self.train(False)
            with torch.inference_mode():
                preds_nexttoken = self.predictor.forward(x_nexttoken, z)
        elif self.meta_objective == "prequential":
            preds_nexttoken = self.predictor.forward(x_nexttoken, z)
            self.train(False)
            with torch.inference_mode():
                preds_train = self.predictor.forward(x_train, z)
        else:
            raise ValueError(f"Invalid meta_objective: {self.meta_objective}")
        self.train(mode)

        return preds_train, preds_nexttoken, x_train, x_nexttoken, z

    @beartype
    def training_step(self, data, batch_idx):
        x, task_params = data
        preds_train, preds_nexttoken, x_train, x_nexttoken, z = self.forward(x)
        loss = self.losses_and_metrics(
            preds_train,
            preds_nexttoken,
            x_train,
            x_nexttoken,
            z,
            task_params,
        )
        return loss

    @beartype
    def validation_step(self, data, batch_idx):
        x, task_params = data
        preds_train, preds_nexttoken, x_train, x_nexttoken, z = self.forward(x)
        _ = self.losses_and_metrics(
            preds_train,
            preds_nexttoken,
            x_train,
            x_nexttoken,
            z,
            task_params,
        )

    @torch.inference_mode()
    def on_train_end(self):
        self.log_loss_vs_nsamples(mode="train_tasks")
        self.log_loss_vs_nsamples(mode="val_tasks")
        if self.hparams.log_eff_zdim:
            self.log_effective_zdim(mode="train_tasks")
            self.log_effective_zdim(mode="val_tasks")

    @torch.inference_mode()
    def log_loss_vs_nsamples(self, mode: Literal["train_tasks", "val_tasks"]):
        if self.logger is None:
            return
        # Get the dataloader
        if mode == "train_tasks":
            dl = self.trainer.datamodule.train_dataloader()
        else:
            dl = self.trainer.datamodule.val_dataloader()

        num_tasks = len(dl.dataset)
        n_sample_loss_train, n_sample_loss_nexttoken, n_sample_loss_ood = (
            None,
            None,
            None,
        )
        loss_train_all, loss_nexttoken_all, loss_ood_all = [], [], []
        all_x = {key: [] for key in ["x", "y", "x_ood", "y_ood", "y_pred"]}

        for x, _ in dl:
            x = {name: x[name].to(self.device) for name in x}
            for key in all_x:
                all_x[key].append(x[key])

            (
                preds_train,
                preds_nexttoken,
                x_train,
                x_nexttoken,
                z,
            ) = self.forward(x)
            all_x["y_pred"].append(preds_nexttoken)

            loss_train = self.loss_function(x_train, preds_train)
            loss_nexttoken = self.loss_function(x_nexttoken, preds_nexttoken)
            loss_train_all.append(loss_train)
            loss_nexttoken_all.append(loss_nexttoken)

            loss_train, loss_nexttoken = (
                loss_train.sum(dim=-1) / num_tasks,
                loss_nexttoken.sum(dim=-1) / num_tasks,
            )
            if n_sample_loss_train is None:
                n_sample_loss_train = loss_train
                n_sample_loss_nexttoken = loss_nexttoken
            else:
                n_sample_loss_train += loss_train
                n_sample_loss_nexttoken += loss_nexttoken

            if self.has_ood:
                x_ood = {
                    name: x_nexttoken[f"{name}_ood"].to(self.device)
                    for name in ["x", "y"]
                }
                preds_ood = self.predictor.forward(x_ood, z)
                loss_ood = self.loss_function(x_ood, preds_ood)
                loss_ood_all.append(loss_ood)
                loss_ood = loss_ood.sum(dim=-1) / num_tasks
                if n_sample_loss_ood is None:
                    n_sample_loss_ood = loss_ood
                else:
                    n_sample_loss_ood += loss_ood

        # Concatenate all tensors for each key
        all_x = {key: torch.cat(tensors, dim=1) for key, tensors in all_x.items()}

        loss_train_all = torch.cat(loss_train_all, dim=1)
        loss_nexttoken_all = torch.cat(loss_nexttoken_all, dim=1)
        loss_ood_all = torch.cat(loss_ood_all, dim=1)

        def plot_top_k_losses(k, step):
            for i in range(0, 999, step):
                losses = loss_nexttoken_all[i]
                top_k_indices = torch.topk(losses, k).indices

                x = all_x["x"][i, top_k_indices].cpu().numpy()
                y = all_x["y"][i, top_k_indices].cpu().numpy()
                y_pred = all_x["y_pred"][i, top_k_indices].cpu().numpy()

                fig, axes = plt.subplots(4, k // 4, figsize=(20, 20))
                axes = (
                    axes.flatten()
                )  # Flatten the 2D array of axes for easier indexing
                fig.suptitle(
                    f"Top {k} losses at sample {i+self.hparams.min_train_samples}"
                )

                for j in range(k):
                    ax = axes[j] if k > 1 else axes
                    ax.scatter(x[j], y[j], c="blue", label="Actual", alpha=0.7)
                    ax.scatter(x[j], y_pred[j], c="red", label="Predicted", alpha=0.7)
                    ax.set_title(
                        f"Index {top_k_indices[j].item()}, Loss: {losses[top_k_indices[j]].item():.4f}"
                    )
                    ax.legend()
                    ax.set_xlabel("x")
                    ax.set_ylabel("y")

                plt.tight_layout()
                wandb_image = wandb.Image(fig)
                self.logger.experiment.log(
                    {
                        f"{mode}/top_{k}_losses_plot_sample_{i+self.hparams.min_train_samples}": wandb_image
                    }
                )
                plt.close(fig)

        # Example usage
        plot_top_k_losses(k=16, step=100)

        hist_num_bins = 500
        df = pd.DataFrame(columns=["loss_train", "loss_nexttoken", "loss_ood"])
        print("Building dataframe")
        for i in range(len(n_sample_loss_train)):
            n_samples = i + self.hparams.min_train_samples
            l_train_i = n_sample_loss_train[i].cpu()
            l_nexttoken_i = n_sample_loss_nexttoken[i].cpu()
            n_sample_log = {
                "n_samples": n_samples,
                f"{mode}/n_sample_loss_train": l_train_i,
                f"{mode}/n_sample_loss_nexttoken": l_nexttoken_i,
                f"{mode}/n_sample_loss_train_hist": wandb.Histogram(
                    loss_train_all[i].cpu(), num_bins=hist_num_bins
                ),
                f"{mode}/n_sample_loss_nexttoken_hist": wandb.Histogram(
                    loss_nexttoken_all[i].cpu(), num_bins=hist_num_bins
                ),
            }
            if self.has_ood:
                l_ood_i = n_sample_loss_ood[i].cpu()
                n_sample_log.update({f"{mode}/n_sample_loss_ood": l_ood_i})
                n_sample_log.update(
                    {
                        f"{mode}/n_sample_loss_ood_hist": wandb.Histogram(
                            loss_ood_all[i].cpu(), num_bins=hist_num_bins
                        ),
                    }
                )
            new_df = pd.DataFrame(
                {
                    "n_samples": torch.full((len(loss_train_all[i]),), n_samples),
                    "loss_train": loss_train_all[i].cpu(),
                    "loss_nexttoken": loss_nexttoken_all[i].cpu(),
                    "loss_ood": loss_ood_all[i].cpu(),
                }
            )
            df = pd.concat([df, new_df], ignore_index=True)

            self.logger.experiment.log(n_sample_log)
        print("Finished building dataframe")
        print("Plotting train loss")
        self.plot_and_log_scatter(df, mode, "loss_train")
        print("Plotting next-token loss")
        self.plot_and_log_scatter(df, mode, "loss_nexttoken")
        if self.has_ood:
            print("Plotting OOD loss")
            self.plot_and_log_scatter(df, mode, "loss_ood")

        # table = wandb.Table(dataframe=df)
        # n_sample_l
        # train_plot = wandb.plot.scatter(
        #     table, "n_samples", "loss_train", title="Train Loss"
        # )
        # nexttoken_plot = wandb.plot.scatter(
        #     table, "n_samples", "loss_nexttoken", title="Next-Token Loss"
        # )
        # if self.has_ood:
        #     ood_plot = wandb.plot.scatter(
        #         table, "n_samples", "loss_ood", title="OOD Loss"
        #     )

        # self.logger.experiment.log(
        #     {
        #         f"{mode}/train_scatter_plot": train_plot,
        #         f"{mode}/nexttoken_scatter_plot": nexttoken_plot,
        #         f"{mode}/ood_scatter_plot": ood_plot,
        #     }
        # )

    def plot_and_log_scatter(self, df, mode, ylabel, alpha=0.1, point_size=10):
        fig, ax = plt.subplots(figsize=(10, 6))
        sns.scatterplot(
            data=df, x="n_samples", y=ylabel, alpha=alpha, s=point_size, ax=ax
        )
        ax.set_xscale("log")  # Set x-axis to logarithmic scale
        ax.set_title(f"{ylabel} vs. Number of Samples")
        ax.set_xlabel("Number of Samples")
        ax.set_ylabel(ylabel)

        self.logger.experiment.log({f"{mode}/{ylabel}_scatter_plot": wandb.Image(fig)})

    @torch.inference_mode()
    def log_effective_zdim(self, mode: Literal["train_tasks", "val_tasks"]):
        if self.logger is None:
            return

        # Get the dataloader
        if mode == "train_tasks":
            dl = self.trainer.datamodule.train_dataloader()
        else:
            dl = self.trainer.datamodule.val_dataloader()

        # Collect the matrix of z's
        zs = None
        for x, _ in dl:
            x = {name: x[name].to(self.device) for name in x}
            z = self.context_aggregator.forward(x)
            if len(z) > 1 or "z" not in z or len(z["z"].shape) != 3:
                return  # This function only makes sense for a single z vector
            z = z["z"].cpu()
            z = z[torch.randperm(len(z))[:10]]  # Randomly sample 10 z's to limit RAM
            z = z.view(-1, z.shape[-1])
            if zs is None:
                zs = z
            else:
                zs = torch.cat([zs, z], dim=0)
            if len(zs) > 10000:  # Maximum 10000 z's to limit RAM
                break

        # Compute and log the effective dimensionality of the z's
        _, variance_explained = torch_pca(zs, center=True, percent=True)
        effdim = (variance_explained.sum() ** 2) / (variance_explained**2).sum()
        self.logger.experiment.log({f"{mode}/effective_z-dim": effdim})

    @beartype
    def losses_and_metrics(
        self,
        preds_train: dict[str, Tensor],
        preds_nexttoken: dict[str, Tensor],
        x_train: dict[str, Tensor],
        x_nexttoken: dict[str, Tensor],
        z: dict[str, Tensor],
        task_params: dict[str, Iterable] | None,
    ) -> torch.Tensor:
        """Computes and logs losses and other metrics. Can be subclassed to add more metrics, or modify loss (e.g., adding a task_params prediction loss).

        Args:
            preds_train (dict[str, Tensor]): Training set predictions (samples, tasks, *).
            preds_nexttoken (dict[str, Tensor]): Next-token predictions (samples, tasks, *).
            x_train (dict[str, Tensor]): Inputs/targets for training set predictions (samples, tasks, *).
            x_nexttoken (dict[str, Tensor]): Inputs/targets for next-token predictions (samples, tasks, *).
            z_train (dict[str, Tensor]): Aggregated context for training set predictions (samples, tasks, *).
            z_nexttoken (dict[str, Tensor]): Aggregated context for next-token predictions (samples, tasks, *).
            task_params (dict[str, Iterable] | None): True task parameters (tasks).

        Returns:
            torch.Tensor: Scalar loss to optimize.
        """
        mode = "train_tasks" if self.training else "val_tasks"
        num_tasks = preds_train[list(preds_train.keys())[0]].shape[1]

        # Main losses
        loss_train = self.loss_function(x_train, preds_train).mean()
        loss_nexttoken = self.loss_function(x_nexttoken, preds_nexttoken).mean()
        loss = loss_train if self.meta_objective == "train" else loss_nexttoken
        self.log(f"{mode}/loss_train", loss_train, batch_size=num_tasks)
        self.log(f"{mode}/loss_nexttoken", loss_nexttoken, batch_size=num_tasks)

        return loss

    @abstractmethod
    def loss_function(
        self, target: dict[str, Tensor], preds: dict[str, Tensor]
    ) -> Tensor:
        """Do not average across samples and tasks! Return shape should be

        Args:
            target (dict[str, Tensor]): Inputs/targets (samples, tasks, *).
            preds (dict[str, Tensor]): Predictions (samples, tasks, *).

        Returns:
            Tensor: Losses (samples, tasks).
        """
        pass

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.hparams.lr)

    @property
    def has_ood(self) -> bool:
        return (
            hasattr(self.trainer.datamodule.train_dataset, "has_ood")
            and self.trainer.datamodule.train_dataset.has_ood
        )


class MetaOptimizerImplicit(ABC, LightningModule):
    @beartype
    def __init__(
        self,
        model: ImplicitModel,
        min_train_samples: int = 1,
        lr: float = 1e-4,
    ):
        super().__init__()
        self.save_hyperparameters(ignore=["model"])
        self.save_hyperparameters({"meta_objective": "prequential"})

        self.model = model

    @beartype
    def forward(
        self, x: dict[str, Tensor]
    ) -> tuple[dict[str, Tensor], dict[str, Tensor]]:
        """Return the next-token predictions.

        Args:
            x (dict[str, Tensor]):  Input data, each with shape (samples, tasks, *).

        Returns:
            tuple[dict[str, Tensor], dict[str, Tensor]]: Next-token predictions.
            - next-token predictions: (samples - min_train_samples + 1, tasks, *).
            - x_nexttoken: (samples - min_train_samples + 1, tasks, *).
        """
        preds_nexttoken = self.model.forward(x)
        preds_nexttoken = {
            name: preds_nexttoken[name][self.hparams.min_train_samples - 1 :]
            for name in preds_nexttoken
        }
        x_nexttoken = {
            name: x[name][self.hparams.min_train_samples - 1 :] for name in x
        }
        return preds_nexttoken, x_nexttoken

    @beartype
    def training_step(self, data, batch_idx):
        x, task_params = data
        preds_nexttoken, x_nexttoken = self.forward(x)
        loss = self.losses_and_metrics(preds_nexttoken, x_nexttoken, task_params)
        return loss

    @beartype
    def validation_step(self, data, batch_idx):
        x, task_params = data
        preds_nexttoken, x_nexttoken = self.forward(x)
        _ = self.losses_and_metrics(preds_nexttoken, x_nexttoken, task_params)

    @torch.inference_mode()
    def on_train_end(self):
        self.log_loss_vs_nsamples(mode="train_tasks")
        self.log_loss_vs_nsamples(mode="val_tasks")

    @torch.inference_mode()
    def log_loss_vs_nsamples(self, mode: Literal["train_tasks", "val_tasks"]):
        if self.logger is None:
            return

        # Get the dataloader
        if mode == "train_tasks":
            dl = self.trainer.datamodule.train_dataloader()
        else:
            dl = self.trainer.datamodule.val_dataloader()

        num_tasks = len(dl.dataset)
        n_sample_loss_nexttoken = None
        for x, _ in dl:
            x = {name: x[name].to(self.device) for name in x}
            preds_nexttoken, x_nexttoken = self.forward(x)
            loss = self.loss_function(x_nexttoken, preds_nexttoken)
            loss = loss.sum(dim=-1) / num_tasks

            if n_sample_loss_nexttoken is None:
                n_sample_loss_nexttoken = loss
            else:
                n_sample_loss_nexttoken += loss

        for i in range(len(n_sample_loss_nexttoken)):
            n_samples = i + self.hparams.min_train_samples - 1
            l = n_sample_loss_nexttoken[i].cpu()
            self.logger.experiment.log(
                {"n_samples": n_samples, f"{mode}/n_sample_loss_nexttoken": l}
            )

    @beartype
    def losses_and_metrics(
        self,
        preds_nexttoken: dict[str, Tensor],
        x_nexttoken: dict[str, Tensor],
        task_params: dict[str, Iterable] | None,
    ) -> torch.Tensor:
        """Computes and logs losses and other metrics. Can be subclassed to add more metrics, or modify loss.

        Args:
            preds_nexttoken (dict[str, Tensor]): Next-token predictions (samples, tasks, *).
            x_nexttoken (dict[str, Tensor]): Inputs/targets for next-token predictions (samples, tasks, *).
            task_params (dict[str, Iterable] | None): True task parameters (tasks).

        Returns:
            torch.Tensor: Scalar loss to optimize.
        """
        mode = "train_tasks" if self.training else "val_tasks"
        num_tasks = preds_nexttoken[list(preds_nexttoken.keys())[0]].shape[1]

        # Main loss
        loss = self.loss_function(x_nexttoken, preds_nexttoken).mean()
        self.log(f"{mode}/loss_nexttoken", loss, batch_size=num_tasks)

        return loss

    @abstractmethod
    def loss_function(
        self, target: dict[str, Tensor], preds: dict[str, Tensor]
    ) -> Tensor:
        """Do not average across samples and tasks! Return shape should be

        Args:
            target (dict[str, Tensor]): Inputs/targets (samples, tasks, *).
            preds (dict[str, Tensor]): Predictions (samples, tasks, *).

        Returns:
            Tensor: Losses (samples, tasks).
        """
        pass

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=self.hparams.lr)
