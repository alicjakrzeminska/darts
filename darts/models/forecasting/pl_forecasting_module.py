"""
This file contains abstract classes for deterministic and probabilistic PyTorch Lightning Modules
"""

from abc import ABC, abstractmethod
from functools import wraps
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import pytorch_lightning as pl
import torch
import torch.nn as nn
import torchmetrics
from joblib import Parallel, delayed

from darts.logging import get_logger, raise_if, raise_log
from darts.models.components.layer_norm_variants import RINorm
from darts.timeseries import TimeSeries
from darts.utils.likelihood_models import Likelihood
from darts.utils.timeseries_generation import _build_forecast_series
from darts.utils.torch import MonteCarloDropout

logger = get_logger(__name__)

# Check whether we are running pytorch-lightning >= 1.6.0 or not:
tokens = pl.__version__.split(".")
pl_160_or_above = int(tokens[0]) > 1 or int(tokens[0]) == 1 and int(tokens[1]) >= 6


def io_processor(forward):
    """Applies some input / output processing to PLForecastingModule.forward.
    Note that this wrapper must be added to each of PLForecastinModule's subclasses forward methods.
    Here is an example how to add the decorator:

    ```python
        @io_processor
        def forward(self, *args, **kwargs)
            pass
    ```

    Applies
    -------
    Reversible Instance Normalization
        normalizes batch input target features, and inverse transform the forward output back to the original scale
    """

    @wraps(forward)
    def forward_wrapper(self, *args, **kwargs):
        if not self.use_reversible_instance_norm:
            return forward(self, *args, **kwargs)

        # x is input batch tuple which by definition has the past features in the first element starting with the
        # first n target features
        # assuming `args[0][0]` is torch.Tensor we could clone it to prevent target re-normalization
        x: Tuple = args[0][0].clone()
        # apply reversible instance normalization
        x[:, :, : self.n_targets] = self.rin(x[:, :, : self.n_targets])
        # run the forward pass
        out = forward(self, *((x, *args[0][1:]), *args[1:]), **kwargs)
        # inverse transform target output back to original scale; by definition the first output
        if isinstance(out, tuple):
            return self.rin.inverse(out[0]), *out[1:]
        else:
            return self.rin.inverse(out)

    return forward_wrapper


class PLForecastingModule(pl.LightningModule, ABC):
    @abstractmethod
    def __init__(
        self,
        input_chunk_length: int,
        output_chunk_length: int,
        train_sample_shape: Optional[Tuple] = None,
        loss_fn: nn.modules.loss._Loss = nn.MSELoss(),
        torch_metrics: Optional[
            Union[torchmetrics.Metric, torchmetrics.MetricCollection]
        ] = None,
        likelihood: Optional[Likelihood] = None,
        optimizer_cls: torch.optim.Optimizer = torch.optim.Adam,
        optimizer_kwargs: Optional[Dict] = None,
        lr_scheduler_cls: Optional[torch.optim.lr_scheduler._LRScheduler] = None,
        lr_scheduler_kwargs: Optional[Dict] = None,
        use_reversible_instance_norm: bool = False,
    ) -> None:
        """
        PyTorch Lightning-based Forecasting Module.

        This class is meant to be inherited to create a new PyTorch Lightning-based forecasting module.
        When subclassing this class, please make sure to add the following methods with the given signatures:
            - :func:`PLTorchForecastingModel.__init__()`
            - :func:`PLTorchForecastingModel.forward()`
            - :func:`PLTorchForecastingModel._produce_train_output()`
            - :func:`PLTorchForecastingModel._get_batch_prediction()`

        In subclass `MyModel`'s :func:`__init__` function call ``super(MyModel, self).__init__(**kwargs)`` where
        ``kwargs`` are the parameters of :class:`PLTorchForecastingModel`.

        Parameters
        ----------
        input_chunk_length
            Number of time steps in the past to take as a model input (per chunk). Applies to the target
            series, and past and/or future covariates (if the model supports it).
        output_chunk_length
            Number of time steps predicted at once (per chunk) by the internal model. Also, the number of future values
            from future covariates to use as a model input (if the model supports future covariates). It is not the same
            as forecast horizon `n` used in `predict()`, which is the desired number of prediction points generated
            using either a one-shot- or auto-regressive forecast. Setting `n <= output_chunk_length` prevents
            auto-regression. This is useful when the covariates don't extend far enough into the future, or to prohibit
            the model from using future values of past and / or future covariates for prediction (depending on the
            model's covariate support).
        train_sample_shape
            Shape of the model's input, used to instantiate model without calling ``fit_from_dataset`` and
            perform sanity check on new training/inference datasets used for re-training or prediction.
        loss_fn
            PyTorch loss function used for training.
            This parameter will be ignored for probabilistic models if the ``likelihood`` parameter is specified.
            Default: ``torch.nn.MSELoss()``.
        torch_metrics
            A torch metric or a ``MetricCollection`` used for evaluation. A full list of available metrics can be found
            at https://torchmetrics.readthedocs.io/en/latest/. Default: ``None``.
        likelihood
            One of Darts' :meth:`Likelihood <darts.utils.likelihood_models.Likelihood>` models to be used for
            probabilistic forecasts. Default: ``None``.
        optimizer_cls
            The PyTorch optimizer class to be used. Default: ``torch.optim.Adam``.
        optimizer_kwargs
            Optionally, some keyword arguments for the PyTorch optimizer (e.g., ``{'lr': 1e-3}``
            for specifying a learning rate). Otherwise the default values of the selected ``optimizer_cls``
            will be used. Default: ``None``.
        lr_scheduler_cls
            Optionally, the PyTorch learning rate scheduler class to be used. Specifying ``None`` corresponds
            to using a constant learning rate. Default: ``None``.
        lr_scheduler_kwargs
            Optionally, some keyword arguments for the PyTorch learning rate scheduler. Default: ``None``.
        use_reversible_instance_norm
            Whether to use reversible instance normalization `RINorm` against distribution shift as shown in [1]_.
            It is only applied to the features of the target series and not the covariates.

        References
        ----------
        .. [1] T. Kim et al. "Reversible Instance Normalization for Accurate Time-Series Forecasting against
                Distribution Shift", https://openreview.net/forum?id=cGDAkQo1C0p
        """
        super().__init__()

        # save hyper parameters for saving/loading
        self.save_hyperparameters(ignore=["loss_fn", "torch_metrics"])

        raise_if(
            input_chunk_length is None or output_chunk_length is None,
            "Both `input_chunk_length` and `output_chunk_length` must be passed to `PLForecastingModule`",
            logger,
        )

        self.input_chunk_length = input_chunk_length
        # output_chunk_length is a property
        self._output_chunk_length = output_chunk_length

        # define the loss function
        self.criterion = loss_fn
        # by default models are deterministic (i.e. not probabilistic)
        self.likelihood = likelihood

        # saved in checkpoint to be able to instantiate a model without calling fit_from_dataset
        self.train_sample_shape = train_sample_shape
        self.n_targets = (
            train_sample_shape[0][1] if train_sample_shape is not None else 1
        )

        # persist optimiser and LR scheduler parameters
        self.optimizer_cls = optimizer_cls
        self.optimizer_kwargs = dict() if optimizer_kwargs is None else optimizer_kwargs
        self.lr_scheduler_cls = lr_scheduler_cls
        self.lr_scheduler_kwargs = (
            dict() if lr_scheduler_kwargs is None else lr_scheduler_kwargs
        )

        # convert torch_metrics to torchmetrics.MetricCollection
        torch_metrics = self.configure_torch_metrics(torch_metrics)
        self.train_metrics = torch_metrics.clone(prefix="train_")
        self.val_metrics = torch_metrics.clone(prefix="val_")

        # reversible instance norm
        self.use_reversible_instance_norm = use_reversible_instance_norm
        if use_reversible_instance_norm:
            self.rin = RINorm(input_dim=self.n_targets)
        else:
            self.rin = None

        # initialize prediction parameters
        self.pred_n: Optional[int] = None
        self.pred_num_samples: Optional[int] = None
        self.pred_roll_size: Optional[int] = None
        self.pred_batch_size: Optional[int] = None
        self.pred_n_jobs: Optional[int] = None
        self.predict_likelihood_parameters: Optional[bool] = None

    @property
    def first_prediction_index(self) -> int:
        """
        Returns the index of the first predicted within the output of self.model.
        """
        return 0

    @abstractmethod
    def forward(self, *args, **kwargs) -> Any:
        super().forward(*args, **kwargs)

    def training_step(self, train_batch, batch_idx) -> torch.Tensor:
        """performs the training step"""
        output = self._produce_train_output(train_batch[:-1])
        target = train_batch[
            -1
        ]  # By convention target is always the last element returned by datasets
        loss = self._compute_loss(output, target)
        self.log(
            "train_loss",
            loss,
            batch_size=train_batch[0].shape[0],
            prog_bar=True,
            sync_dist=True,
        )
        self._calculate_metrics(output, target, self.train_metrics)
        return loss

    def validation_step(self, val_batch, batch_idx) -> torch.Tensor:
        """performs the validation step"""
        output = self._produce_train_output(val_batch[:-1])
        target = val_batch[-1]
        loss = self._compute_loss(output, target)
        self.log(
            "val_loss",
            loss,
            batch_size=val_batch[0].shape[0],
            prog_bar=True,
            sync_dist=True,
        )
        self._calculate_metrics(output, target, self.val_metrics)
        return loss

    def predict_step(
        self, batch: Tuple, batch_idx: int, dataloader_idx: Optional[int] = None
    ) -> Sequence[TimeSeries]:
        """performs the prediction step

        batch
            output of Darts' :class:`InferenceDataset` - tuple of ``(past_target, past_covariates,
            historic_future_covariates, future_covariates, future_past_covariates, input_timeseries)``
        batch_idx
            the batch index of the current batch
        dataloader_idx
            the dataloader index
        """
        input_data_tuple, batch_input_series, batch_pred_starts = (
            batch[:-2],
            batch[-2],
            batch[-1],
        )

        # number of individual series to be predicted in current batch
        num_series = input_data_tuple[0].shape[0]

        # number of times the input tensor should be tiled to produce predictions for multiple samples
        # this variable is larger than 1 only if the batch_size is at least twice as large as the number
        # of individual time series being predicted in current batch (`num_series`)
        batch_sample_size = min(
            max(self.pred_batch_size // num_series, 1), self.pred_num_samples
        )

        # counts number of produced prediction samples for every series to be predicted in current batch
        sample_count = 0

        # repeat prediction procedure for every needed sample
        batch_predictions = []
        while sample_count < self.pred_num_samples:

            # make sure we don't produce too many samples
            if sample_count + batch_sample_size > self.pred_num_samples:
                batch_sample_size = self.pred_num_samples - sample_count

            # stack multiple copies of the tensors to produce probabilistic forecasts
            input_data_tuple_samples = self._sample_tiling(
                input_data_tuple, batch_sample_size
            )

            # get predictions for 1 whole batch (can include predictions of multiple series
            # and for multiple samples if a probabilistic forecast is produced)
            batch_prediction = self._get_batch_prediction(
                self.pred_n, input_data_tuple_samples, self.pred_roll_size
            )

            # reshape from 3d tensor (num_series x batch_sample_size, ...)
            # into 4d tensor (batch_sample_size, num_series, ...), where dim 0 represents the samples
            out_shape = batch_prediction.shape
            batch_prediction = batch_prediction.reshape(
                (
                    batch_sample_size,
                    num_series,
                )
                + out_shape[1:]
            )

            # save all predictions and update the `sample_count` variable
            batch_predictions.append(batch_prediction)
            sample_count += batch_sample_size

        # concatenate the batch of samples, to form self.pred_num_samples samples
        batch_predictions = torch.cat(batch_predictions, dim=0)
        batch_predictions = batch_predictions.cpu().detach().numpy()

        ts_forecasts = Parallel(n_jobs=self.pred_n_jobs)(
            delayed(_build_forecast_series)(
                [batch_prediction[batch_idx] for batch_prediction in batch_predictions],
                input_series,
                custom_columns=(
                    self.likelihood.likelihood_components_names(input_series)
                    if self.predict_likelihood_parameters
                    else None
                ),
                with_static_covs=False if self.predict_likelihood_parameters else True,
                with_hierarchy=False if self.predict_likelihood_parameters else True,
                pred_start=pred_start,
            )
            for batch_idx, (input_series, pred_start) in enumerate(
                zip(batch_input_series, batch_pred_starts)
            )
        )
        return ts_forecasts

    def set_predict_parameters(
        self,
        n: int,
        num_samples: int,
        roll_size: int,
        batch_size: int,
        n_jobs: int,
        predict_likelihood_parameters: bool,
    ) -> None:
        """to be set from TorchForecastingModel before calling trainer.predict() and reset at self.on_predict_end()"""
        self.pred_n = n
        self.pred_num_samples = num_samples
        self.pred_roll_size = roll_size
        self.pred_batch_size = batch_size
        self.pred_n_jobs = n_jobs
        self.predict_likelihood_parameters = predict_likelihood_parameters

    def _compute_loss(self, output, target):
        # output is of shape (batch_size, n_timesteps, n_components, n_params)
        if self.likelihood:
            return self.likelihood.compute_loss(output, target)
        else:
            # If there's no likelihood, nr_params=1, and we need to squeeze out the
            # last dimension of model output, for properly computing the loss.
            return self.criterion(output.squeeze(dim=-1), target)

    def _calculate_metrics(self, output, target, metrics):
        if not len(metrics):
            return

        if self.likelihood:
            _metric = metrics(self.likelihood.sample(output), target)
        else:
            # If there's no likelihood, nr_params=1, and we need to squeeze out the
            # last dimension of model output, for properly computing the metric.
            _metric = metrics(output.squeeze(dim=-1), target)

        self.log_dict(
            _metric,
            on_epoch=True,
            on_step=False,
            logger=True,
            prog_bar=True,
            sync_dist=True,
        )

    def configure_optimizers(self):
        """configures optimizers and learning rate schedulers for model optimization."""

        # A utility function to create optimizer and lr scheduler from desired classes
        def _create_from_cls_and_kwargs(cls, kws):
            try:
                return cls(**kws)
            except (TypeError, ValueError) as e:
                raise_log(
                    ValueError(
                        "Error when building the optimizer or learning rate scheduler;"
                        "please check the provided class and arguments"
                        "\nclass: {}"
                        "\narguments (kwargs): {}"
                        "\nerror:\n{}".format(cls, kws, e)
                    ),
                    logger,
                )

        # Create the optimizer and (optionally) the learning rate scheduler
        # we have to create copies because we cannot save model.parameters into object state (not serializable)
        optimizer_kws = {k: v for k, v in self.optimizer_kwargs.items()}
        optimizer_kws["params"] = self.parameters()

        optimizer = _create_from_cls_and_kwargs(self.optimizer_cls, optimizer_kws)

        if self.lr_scheduler_cls is not None:
            lr_sched_kws = {k: v for k, v in self.lr_scheduler_kwargs.items()}
            lr_sched_kws["optimizer"] = optimizer

            # lr scheduler can be configured with lightning; defaults below
            lr_config_params = {
                "monitor": "val_loss",
                "interval": "epoch",
                "frequency": 1,
                "strict": True,
                "name": None,
            }
            # update config with user params
            lr_config_params = {
                k: (v if k not in lr_sched_kws else lr_sched_kws.pop(k))
                for k, v in lr_config_params.items()
            }

            lr_scheduler = _create_from_cls_and_kwargs(
                self.lr_scheduler_cls, lr_sched_kws
            )

            return [optimizer], dict({"scheduler": lr_scheduler}, **lr_config_params)
        else:
            return optimizer

    @abstractmethod
    def _produce_train_output(self, input_batch: Tuple) -> torch.Tensor:
        pass

    @abstractmethod
    def _get_batch_prediction(
        self, n: int, input_batch: Tuple, roll_size: int
    ) -> torch.Tensor:
        """
        In charge of applying the recurrent logic for non-recurrent models.
        Should be overwritten by recurrent models.
        """
        pass

    @staticmethod
    def _sample_tiling(input_data_tuple, batch_sample_size):
        tiled_input_data = []
        for tensor in input_data_tuple:
            if tensor is not None:
                tiled_input_data.append(tensor.tile((batch_sample_size, 1, 1)))
            else:
                tiled_input_data.append(None)
        return tuple(tiled_input_data)

    def _get_mc_dropout_modules(self) -> set:
        def recurse_children(children, acc):
            for module in children:
                if isinstance(module, MonteCarloDropout):
                    acc.add(module)
                acc = recurse_children(module.children(), acc)
            return acc

        return recurse_children(self.children(), set())

    def set_mc_dropout(self, active: bool):
        for module in self._get_mc_dropout_modules():
            module.mc_dropout_enabled = active

    @property
    def _is_probabilistic(self) -> bool:
        return self.likelihood is not None or len(self._get_mc_dropout_modules()) > 0

    def _produce_predict_output(self, x: Tuple) -> torch.Tensor:
        if self.likelihood:
            output = self(x)
            if self.predict_likelihood_parameters:
                return self.likelihood.predict_likelihood_parameters(output)
            else:
                return self.likelihood.sample(output)
        else:
            return self(x).squeeze(dim=-1)

    def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        # we must save the dtype for correct parameter precision at loading time
        checkpoint["model_dtype"] = self.dtype
        # we must save the shape of the input to be able to instanciate the model without calling fit_from_dataset
        checkpoint["train_sample_shape"] = self.train_sample_shape
        # we must save the loss to properly restore it when resuming training
        checkpoint["loss_fn"] = self.criterion
        # we must save the metrics to continue outputing them when resuming training
        checkpoint["torch_metrics_train"] = self.train_metrics
        checkpoint["torch_metrics_val"] = self.val_metrics

    def on_load_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        # by default our models are initialized as float32. For other dtypes, we need to cast to the correct precision
        # before parameters are loaded by PyTorch-Lightning
        dtype = checkpoint["model_dtype"]
        self.to_dtype(dtype)

        # restoring attributes necessary to resume from training properly
        if (
            "loss_fn" in checkpoint.keys()
            and "torch_metrics_train" in checkpoint.keys()
        ):
            self.criterion = checkpoint["loss_fn"]
            self.train_metrics = checkpoint["torch_metrics_train"]
            self.val_metrics = checkpoint["torch_metrics_val"]
        else:
            # explicitly indicate to the user that there is a bug
            logger.warning(
                "This checkpoint was generated with darts <= 0.24.0, if a custom loss "
                "was used to train the model, it won't be properly loaded. Similarly, "
                "the torch metrics won't be restored from the checkpoint."
            )

    def to_dtype(self, dtype):
        """Cast module precision (float32 by default) to another precision."""
        if dtype == torch.float16:
            self.half()
        if dtype == torch.float32:
            self.float()
        elif dtype == torch.float64:
            self.double()
        else:
            raise_if(
                True,
                f"Trying to load dtype {dtype}. Loading for this type is not implemented yet. Please report this "
                f"issue on https://github.com/unit8co/darts",
                logger,
            )

    @property
    def epochs_trained(self):
        current_epoch = self.current_epoch

        # For PTL < 1.6.0 we have to adjust:
        if not pl_160_or_above and (self.current_epoch or self.global_step):
            current_epoch += 1

        return current_epoch

    @property
    def output_chunk_length(self) -> Optional[int]:
        """
        Number of time steps predicted at once by the model.
        """
        return self._output_chunk_length

    @staticmethod
    def configure_torch_metrics(
        torch_metrics: Union[torchmetrics.Metric, torchmetrics.MetricCollection]
    ) -> torchmetrics.MetricCollection:
        """process the torch_metrics parameter."""
        if torch_metrics is None:
            torch_metrics = torchmetrics.MetricCollection([])
        elif isinstance(torch_metrics, torchmetrics.Metric):
            torch_metrics = torchmetrics.MetricCollection([torch_metrics])
        elif isinstance(torch_metrics, torchmetrics.MetricCollection):
            pass
        else:
            raise_log(
                AttributeError(
                    "`torch_metrics` only accepts type torchmetrics.Metric or torchmetrics.MetricCollection"
                ),
                logger,
            )
        return torch_metrics


class PLPastCovariatesModule(PLForecastingModule, ABC):
    def _produce_train_output(self, input_batch: Tuple):
        """
        Feeds PastCovariatesTorchModel with input and output chunks of a PastCovariatesSequentialDataset for
        training.

        Parameters:
        ----------
        input_batch
            ``(past_target, past_covariates, static_covariates)``
        """
        past_target, past_covariates, static_covariates = input_batch
        # Currently all our PastCovariates models require past target and covariates concatenated
        inpt = (
            (
                torch.cat([past_target, past_covariates], dim=2)
                if past_covariates is not None
                else past_target
            ),
            static_covariates,
        )
        return self(inpt)

    def _get_batch_prediction(
        self, n: int, input_batch: Tuple, roll_size: int
    ) -> torch.Tensor:
        """
        Feeds PastCovariatesTorchModel with input and output chunks of a PastCovariatesSequentialDataset to forecast
        the next ``n`` target values per target variable.

        Parameters:
        ----------
        n
            prediction length
        input_batch
            ``(past_target, past_covariates, future_past_covariates, static_covariates)``
        roll_size
            roll input arrays after every sequence by ``roll_size``. Initially, ``roll_size`` is equivalent to
            ``self.output_chunk_length``
        """
        dim_component = 2
        (
            past_target,
            past_covariates,
            future_past_covariates,
            static_covariates,
        ) = input_batch

        n_targets = past_target.shape[dim_component]
        n_past_covs = (
            past_covariates.shape[dim_component] if past_covariates is not None else 0
        )

        input_past = torch.cat(
            [ds for ds in [past_target, past_covariates] if ds is not None],
            dim=dim_component,
        )

        out = self._produce_predict_output(x=(input_past, static_covariates))[
            :, self.first_prediction_index :, :
        ]

        batch_prediction = [out[:, :roll_size, :]]
        prediction_length = roll_size

        while prediction_length < n:
            # we want the last prediction to end exactly at `n` into the future.
            # this means we may have to truncate the previous prediction and step
            # back the roll size for the last chunk
            if prediction_length + self.output_chunk_length > n:
                spillover_prediction_length = (
                    prediction_length + self.output_chunk_length - n
                )
                roll_size -= spillover_prediction_length
                prediction_length -= spillover_prediction_length
                batch_prediction[-1] = batch_prediction[-1][:, :roll_size, :]

            # ==========> PAST INPUT <==========
            # roll over input series to contain the latest target and covariates
            input_past = torch.roll(input_past, -roll_size, 1)

            # update target input to include next `roll_size` predictions
            if self.input_chunk_length >= roll_size:
                input_past[:, -roll_size:, :n_targets] = out[:, :roll_size, :]
            else:
                input_past[:, :, :n_targets] = out[:, -self.input_chunk_length :, :]

            # set left and right boundaries for extracting future elements
            if self.input_chunk_length >= roll_size:
                left_past, right_past = prediction_length - roll_size, prediction_length
            else:
                left_past, right_past = (
                    prediction_length - self.input_chunk_length,
                    prediction_length,
                )

            # update past covariates to include next `roll_size` future past covariates elements
            if n_past_covs and self.input_chunk_length >= roll_size:
                input_past[:, -roll_size:, n_targets : n_targets + n_past_covs] = (
                    future_past_covariates[:, left_past:right_past, :]
                )
            elif n_past_covs:
                input_past[:, :, n_targets : n_targets + n_past_covs] = (
                    future_past_covariates[:, left_past:right_past, :]
                )

            # take only last part of the output sequence where needed
            out = self._produce_predict_output(x=(input_past, static_covariates))[
                :, self.first_prediction_index :, :
            ]

            batch_prediction.append(out)
            prediction_length += self.output_chunk_length

        # bring predictions into desired format and drop unnecessary values
        batch_prediction = torch.cat(batch_prediction, dim=1)
        batch_prediction = batch_prediction[:, :n, :]
        return batch_prediction


class PLFutureCovariatesModule(PLForecastingModule, ABC):
    def _get_batch_prediction(
        self, n: int, input_batch: Tuple, roll_size: int
    ) -> torch.Tensor:
        raise NotImplementedError("TBD: Darts doesn't contain such a model yet.")


class PLDualCovariatesModule(PLForecastingModule, ABC):
    def _get_batch_prediction(
        self, n: int, input_batch: Tuple, roll_size: int
    ) -> torch.Tensor:
        raise NotImplementedError(
            "TBD: The only DualCovariatesModel is an RNN with a specific implementation."
        )


class PLMixedCovariatesModule(PLForecastingModule, ABC):
    def _produce_train_output(
        self, input_batch: Tuple
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Feeds MixedCovariatesTorchModel with input and output chunks of a MixedCovariatesSequentialDataset for
        training.

        Parameters:
        ----------
        input_batch
            ``(past_target, past_covariates, historic_future_covariates, future_covariates, static_covariates)``.
        """
        return self(self._process_input_batch(input_batch))

    def _process_input_batch(
        self, input_batch
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[torch.Tensor]]:
        """
        Converts output of MixedCovariatesDataset (training dataset) into an input/past- and
        output/future chunk.

        Parameters
        ----------
        input_batch
            ``(past_target, past_covariates, historic_future_covariates, future_covariates, static_covariates)``.

        Returns
        -------
        tuple
            ``(x_past, x_future, x_static)`` the input/past and output/future chunks.
        """
        (
            past_target,
            past_covariates,
            historic_future_covariates,
            future_covariates,
            static_covariates,
        ) = input_batch
        dim_variable = 2

        x_past = torch.cat(
            [
                tensor
                for tensor in [
                    past_target,
                    past_covariates,
                    historic_future_covariates,
                ]
                if tensor is not None
            ],
            dim=dim_variable,
        )
        return x_past, future_covariates, static_covariates

    def _get_batch_prediction(
        self, n: int, input_batch: Tuple, roll_size: int
    ) -> torch.Tensor:
        """
        Feeds MixedCovariatesModel with input and output chunks of a MixedCovariatesSequentialDataset to forecast
        the next ``n`` target values per target variable.

        Parameters
        ----------
        n
            prediction length
        input_batch
            (past_target, past_covariates, historic_future_covariates, future_covariates, future_past_covariates)
        roll_size
            roll input arrays after every sequence by ``roll_size``. Initially, ``roll_size`` is equivalent to
            ``self.output_chunk_length``
        """

        dim_component = 2
        (
            past_target,
            past_covariates,
            historic_future_covariates,
            future_covariates,
            future_past_covariates,
            static_covariates,
        ) = input_batch

        n_targets = past_target.shape[dim_component]
        n_past_covs = (
            past_covariates.shape[dim_component] if past_covariates is not None else 0
        )
        n_future_covs = (
            future_covariates.shape[dim_component]
            if future_covariates is not None
            else 0
        )

        input_past, input_future, input_static = self._process_input_batch(
            (
                past_target,
                past_covariates,
                historic_future_covariates,
                (
                    future_covariates[:, :roll_size, :]
                    if future_covariates is not None
                    else None
                ),
                static_covariates,
            )
        )

        out = self._produce_predict_output(x=(input_past, input_future, input_static))[
            :, self.first_prediction_index :, :
        ]

        batch_prediction = [out[:, :roll_size, :]]
        prediction_length = roll_size

        while prediction_length < n:
            # we want the last prediction to end exactly at `n` into the future.
            # this means we may have to truncate the previous prediction and step
            # back the roll size for the last chunk
            if prediction_length + self.output_chunk_length > n:
                spillover_prediction_length = (
                    prediction_length + self.output_chunk_length - n
                )
                roll_size -= spillover_prediction_length
                prediction_length -= spillover_prediction_length
                batch_prediction[-1] = batch_prediction[-1][:, :roll_size, :]

            # ==========> PAST INPUT <==========
            # roll over input series to contain the latest target and covariates
            input_past = torch.roll(input_past, -roll_size, 1)

            # update target input to include next `roll_size` predictions
            if self.input_chunk_length >= roll_size:
                input_past[:, -roll_size:, :n_targets] = out[:, :roll_size, :]
            else:
                input_past[:, :, :n_targets] = out[:, -self.input_chunk_length :, :]

            # set left and right boundaries for extracting future elements
            if self.input_chunk_length >= roll_size:
                left_past, right_past = prediction_length - roll_size, prediction_length
            else:
                left_past, right_past = (
                    prediction_length - self.input_chunk_length,
                    prediction_length,
                )

            # update past covariates to include next `roll_size` future past covariates elements
            if n_past_covs and self.input_chunk_length >= roll_size:
                input_past[:, -roll_size:, n_targets : n_targets + n_past_covs] = (
                    future_past_covariates[:, left_past:right_past, :]
                )
            elif n_past_covs:
                input_past[:, :, n_targets : n_targets + n_past_covs] = (
                    future_past_covariates[:, left_past:right_past, :]
                )

            # update historic future covariates to include next `roll_size` future covariates elements
            if n_future_covs and self.input_chunk_length >= roll_size:
                input_past[:, -roll_size:, n_targets + n_past_covs :] = (
                    future_covariates[:, left_past:right_past, :]
                )
            elif n_future_covs:
                input_past[:, :, n_targets + n_past_covs :] = future_covariates[
                    :, left_past:right_past, :
                ]

            # ==========> FUTURE INPUT <==========
            left_future, right_future = (
                right_past,
                right_past + self.output_chunk_length,
            )
            # update future covariates to include next `roll_size` future covariates elements
            if n_future_covs:
                input_future = future_covariates[:, left_future:right_future, :]

            # take only last part of the output sequence where needed
            out = self._produce_predict_output(
                x=(input_past, input_future, input_static)
            )[:, self.first_prediction_index :, :]

            batch_prediction.append(out)
            prediction_length += self.output_chunk_length

        # bring predictions into desired format and drop unnecessary values
        batch_prediction = torch.cat(batch_prediction, dim=1)
        batch_prediction = batch_prediction[:, :n, :]
        return batch_prediction


class PLSplitCovariatesModule(PLForecastingModule, ABC):
    def _get_batch_prediction(
        self, n: int, input_batch: Tuple, roll_size: int
    ) -> torch.Tensor:
        raise NotImplementedError("TBD: Darts doesn't contain such a model yet.")
