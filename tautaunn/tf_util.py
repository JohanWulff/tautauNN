# coding: utf-8

from __future__ import annotations

import os
import io
from typing import Callable, Any

import numpy as np
import tensorflow as tf
from tensorflow.experimental import numpy as tnp
from keras.src.utils.io_utils import print_msg
import sklearn.metrics
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from tautaunn.util import plot_confusion_matrix, plot_class_outputs


debug_layer = tf.autograph.experimental.do_not_convert


def get_device(device: str = "cpu", num_device: int = 0) -> tf.device:
    if device == "gpu":
        gpus = tf.config.experimental.list_physical_devices("GPU")
        if gpus:
            try:
                tf.config.experimental.set_memory_growth(gpus[num_device], True)
                return tf.device(f"/device:GPU:{num_device}")
            except RuntimeError as e:
                print(e)
        else:
            print("no gpu found, falling back to cpu")

    return tf.device(f"/device:CPU:{num_device}")


def fig_to_image_tensor(fig) -> tf.Tensor:
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    image = tf.image.decode_png(buf.getvalue(), channels=4)
    image = tf.expand_dims(image, 0)
    return image


class ClassificationModelWithValidationBuffers(tf.keras.Model):
    """
    Custom model that saves labels and predictions during validation and resets them before starting a new round.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # buffers for labels and predictions that are filled after validation
        self.buffer_y = self._create_validation_buffer()
        self.buffer_y_pred = self._create_validation_buffer()
        self.buffer_y_empty = self._create_validation_buffer()

    def _create_validation_buffer(self):
        return tf.Variable(
            tnp.empty((0, self.output_shape[-1]), dtype=tf.float32),
            shape=[None, self.output_shape[-1]],
            trainable=False,
        )

    def _reset_validation_buffer(self):
        self.buffer_y.assign(self.buffer_y_empty)
        self.buffer_y_pred.assign(self.buffer_y_empty)

    def _extend_validation_buffer(self, y, y_pred):
        self.buffer_y.assign(tf.concat([self.buffer_y, y], axis=0))
        self.buffer_y_pred.assign(tf.concat([self.buffer_y_pred, y_pred], axis=0))

    def test_on_batch(self, *args, **kwargs):
        self._reset_validation_buffer()
        return super().test_on_batch(*args, **kwargs)

    def evaluate(self, *args, **kwargs):
        self._reset_validation_buffer()
        return super().evaluate(*args, **kwargs)

    def test_step(self, data):
        x, y, sample_weight = tf.keras.utils.unpack_x_y_sample_weight(data)
        y_pred = self(x, training=False)

        self._extend_validation_buffer(y, y_pred)

        self.compute_loss(x, y, y_pred, sample_weight)
        return self.compute_metrics(x, y, y_pred, sample_weight)


class L2Metric(tf.keras.metrics.Metric):

    def __init__(self, model: tf.keras.Model, name: str = "l2", **kwargs) -> None:
        super().__init__(name=name, **kwargs)

        # store kernels and l2 norms of dense layers
        self.kernels: list[tf.Tensor] = []
        self.norms: list[np.ndarray] = []
        for layer in model.layers:
            if isinstance(layer, tf.keras.layers.Dense) and layer.kernel_regularizer is not None:
                self.kernels.append(layer.kernel)
                self.norms.append(layer.kernel_regularizer.l2)

        # book the l2 metric
        self.l2: tf.Variable = self.add_weight(name="l2", initializer="zeros")

    def update_state(self, y_true: tf.Tensor, y_pred: tf.Tensor, sample_weight: tf.Tensor | None = None) -> None:
        self.l2.assign(tf.add_n([tf.reduce_sum(k**2) * n for k, n in zip(self.kernels, self.norms)]))

    def result(self) -> tf.Tensor:
        return self.l2

    def reset_states(self) -> None:
        self.l2.assign(0.0)


class ReduceLRAndStop(tf.keras.callbacks.Callback):

    def __init__(
        self,
        monitor: str = "val_loss",
        min_delta: float = 1.0e-5,
        mode: str = "min",
        lr_patience: int = 10,
        lr_factor: float = 0.1,
        lr_reductions: int = 1,
        es_patience: int = 1,
        restore_best_weights: bool = True,
        start_from_epoch: int = 0,
        verbose: int = 0,
        **kwargs,
    ):
        super().__init__(**kwargs)

        # some checks
        if mode not in ["min", "max"]:
            raise ValueError(f"{self.__class__.__name__} received unknown mode ({mode})")
        if lr_patience < 0:
            raise ValueError(f"{self.__class__.__name__} received lr_patience < 0 ({lr_patience})")
        if lr_factor >= 1.0:
            raise ValueError(f"{self.__class__.__name__} received lr_factor >= 1 ({lr_factor})")
        if lr_reductions < 1:
            raise ValueError(f"{self.__class__.__name__} received lr_reductions < 1 ({lr_reductions})")
        if es_patience < 0:
            raise ValueError(f"{self.__class__.__name__} received es_patience < 0 ({es_patience})")

        # set attributes
        self.monitor = monitor
        self.min_delta = abs(float(min_delta))
        self.mode = mode
        self.lr_patience = int(lr_patience)
        self.lr_factor = float(lr_factor)
        self.lr_reductions = int(lr_reductions)
        self.es_patience = int(es_patience)
        self.restore_best_weights = restore_best_weights
        self.start_from_epoch = int(start_from_epoch)
        self.verbose = int(verbose)

        # state
        self.wait: int = 0
        self.lr_counter: int = 0
        self.best_epoch: int = -1
        self.best_weights: tuple[tf.Tensor, ...] | None = None
        self.best_metric: float = np.nan
        self.monitor_op: Callable[[float, float], bool] | None = None

        self._reset()

    def _reset(self) -> None:
        self.wait = 0
        self.lr_counter = 0
        self.best_epoch = -1
        self.best_weights = None

        if self.mode == "min":
            self.best_metric = np.inf
            self.monitor_op = lambda cur, best: (best - cur) > self.min_delta
        else:  # "max"
            self.best_metric = -np.inf
            self.monitor_op = lambda cur, best: (cur - best) > self.min_delta

    def on_train_begin(self, logs: dict[str, Any] | None = None) -> None:
        self._reset()

    def on_epoch_end(self, epoch: int, logs: dict[str, Any] | None = None) -> None:
        # add the current learning rate to the logs
        logs = logs or {}
        logs["lr"] = tf.keras.backend.get_value(self.model.optimizer.lr)

        # do nothing if configured to skip epochs
        if epoch < self.start_from_epoch:
            return

        # do nothing when no metric is available yet
        value = self.get_monitor_value(logs)
        if value is None:
            return

        # helper to get a newline only for the first invocation
        nls = {"nl": "\n"}
        nl = lambda: nls.pop("nl", "")

        # new best value?
        if self.best_metric is None or self.monitor_op(value, self.best_metric):
            self.best_metric = value
            self.best_weights = self.model.get_weights()
            self.best_epoch = epoch
            self.wait = 0
            if self.verbose >= 2:
                print_msg(f"{nl()}{self.__class__.__name__}: recorded new best value of {value:.5f}")
            return

        self.wait += 1
        if self.verbose >= 2:
            print_msg(
                f"{nl()}{self.__class__.__name__}: wait counter set to {self.wait} "
                f"(LR patience: {self.lr_patience}, ES patience {self.es_patience})",
            )

        # drop learning rate?
        if self.lr_counter < self.lr_reductions:
            # drop
            if self.wait > self.lr_patience:
                # reduce
                logs["lr"] *= self.lr_factor
                tf.keras.backend.set_value(self.model.optimizer.lr, logs["lr"])
                self.lr_counter += 1
                self.wait = 0
                if self.verbose >= 1:
                    print_msg(
                        f"{nl()}{self.__class__.__name__}: reducing learning rate to {logs['lr']:.2e} "
                        f"({self.lr_counter} / {self.lr_reductions}), best metric is {self.best_metric:.5f}",
                    )
                    if self.lr_counter == self.lr_reductions:
                        print_msg(
                            f"{nl()}{self.__class__.__name__}: learning rate reductions exhausted, "
                            f"from now on checking early stopping with patience {self.es_patience}",
                        )
            return

        # stop training?
        if self.wait > self.es_patience:
            self.model.stop_training = True
            if self.verbose >= 1:
                print_msg(f"{nl()}{self.__class__.__name__}: early stopping triggered")

    def on_train_end(self, logs: dict[str, Any] | None = None) -> None:
        if self.best_weights is not None:
            self.model.set_weights(self.best_weights)
            if self.verbose >= 1:
                print_msg(f"{self.__class__.__name__}: recovered best weights from epoch {self.best_epoch + 1}")

    def get_monitor_value(self, logs: dict[str, Any]) -> float | int:
        logs = logs or {}
        value = logs.get(self.monitor)
        if value is None:
            print_msg(f"{self.__class__.__name__}: metric '{self.monitor}' not available, found {','.join(list(logs))}")
        return value


class LivePlotWriter(tf.keras.callbacks.Callback):

    def __init__(
        self,
        log_dir: str,
        class_names: list[str],
        validate_every: int = 1,
        name="confusion",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)

        # attributes
        self.log_dir: str = log_dir
        self.class_names: list[str] = class_names
        self.validate_every: int = validate_every

        # state
        self.file_writer: tf.summary.SummaryWriter = tf.summary.create_file_writer(os.path.join(log_dir, "validation"))
        self.counter: int = 0

    def on_test_end(self, logs: dict[str, Any] | None = None) -> None:
        self.counter += 1

        if getattr(self.model, "buffer_y", None) is None or getattr(self.model, "buffer_y_pred", None) is None:
            print_msg(
                f"\n{self.__class__.__name__} requires model.buffer_y and model.buffer_y_pred to be set, "
                "not writing summary images",
            )
            return

        # get data
        y = self.model.buffer_y.numpy()
        y_pred = self.model.buffer_y_pred.numpy()

        # confusion matrix
        true_classes = np.argmax(y, axis=1)
        pred_classes = np.argmax(y_pred, axis=1)
        cm = sklearn.metrics.confusion_matrix(true_classes, pred_classes, normalize="true")
        cm_image = fig_to_image_tensor(plot_confusion_matrix(cm, self.class_names, colorbar=False)[0])

        # output distributions
        out_imgs = [
            fig_to_image_tensor(plot_class_outputs(y_pred, y, i, self.class_names)[0])
            for i in range(len(self.class_names))
        ]

        with self.file_writer.as_default():
            step = self.counter * self.validate_every
            tf.summary.image("epoch_confusion_matrix", cm_image, step=step)
            for i, img in enumerate(out_imgs):
                tf.summary.image(f"epoch_output_distribution_{self.class_names[i]}", img, step=step)
            self.file_writer.flush()


class EmbeddingEncoder(tf.keras.layers.Layer):

    def __init__(self, expected_inputs, keys_dtype=tf.int32, values_dtype=tf.int32, **kwargs):
        super().__init__(**kwargs)

        self.expected_inputs = expected_inputs
        self.keys_dtype = keys_dtype
        self.values_dtype = values_dtype

        self.n_inputs = len(expected_inputs)
        self.tables = []

    def get_config(self):
        config = super().get_config()
        config["expected_inputs"] = self.expected_inputs
        config["keys_dtype"] = self.keys_dtype
        config["values_dtype"] = self.values_dtype
        return config

    def build(self, input_shape):
        for i, keys in enumerate(self.expected_inputs):
            keys = tf.constant(keys, dtype=self.keys_dtype)
            offset = sum(map(len, self.expected_inputs[:i]))
            values = tf.constant(list(range(len(keys))), dtype=self.values_dtype) + offset
            table = tf.lookup.StaticHashTable(tf.lookup.KeyValueTensorInitializer(keys, values), -1)
            self.tables.append(table)

        return super().build(input_shape)

    def call(self, x):
        return tf.concat(
            [
                self.tables[i].lookup(x[..., i:i + 1])
                for i in range(self.n_inputs)
            ],
            axis=1,
        )