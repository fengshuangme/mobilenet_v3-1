# MobileNet V3 implementation.
# Paper: https://arxiv.org/pdf/1905.02244.pdf

from typing import Tuple, Union, Dict
import collections

import tensorflow as tf
from tensorflow import keras

import mobilenet_v3_configs as conf

_REGULARIZER = keras.regularizers.l2(1e-5)
_CONV_INITIALIZER = keras.initializers.VarianceScaling(seed=42)

# @tf.function
def hard_sigmoid(x):
    return tf.nn.relu6(x + 3.0) / 6.0


def hard_swish(x):
    return hard_sigmoid(x) * x


def _activation(x, name: str = "relu") -> keras.layers.Layer:
    if name == "relu":
        return tf.nn.relu(x)
    elif name == "hardswish":
        return hard_swish(x)
    else:
        raise ValueError(f"Unsupported activation: {name}.")


def _round_to_multiple_of(val, divisor, round_up_bias=0.9):
    """ Asymmetric rounding to make `val` divisible by `divisor`. With default
    bias, will round up, unless the number is no more than 10% greater than the
    smaller divisible value, i.e. (83, 8) -> 80, but (84, 8) -> 88. """
    assert 0.0 < round_up_bias < 1.0
    new_val = max(divisor, int(val + divisor / 2) // divisor * divisor)
    return new_val if new_val >= round_up_bias * val else new_val + divisor


class _SqueezeAndExcitation(keras.layers.Layer):
    def __init__(self, channels: int, se_ratio: float):
        if se_ratio <= 0.0:
            raise ValueError("Squeeze and excitation depth ratio must be positive.")
        super().__init__()
        self.channels = channels
        self.se_ratio = se_ratio
        reduced_ch = _round_to_multiple_of(channels * se_ratio, 8)
        self.reduce = keras.layers.Conv2D(
            reduced_ch,
            1,
            padding="same",
            kernel_initializer=_CONV_INITIALIZER,
            kernel_regularizer=_REGULARIZER,
            use_bias=True,
        )
        self.expand = keras.layers.Conv2D(
            channels,
            1,
            padding="same",
            kernel_initializer=_CONV_INITIALIZER,
            kernel_regularizer=_REGULARIZER,
            use_bias=True,
        )

    def call(self, x):
        y = tf.math.reduce_mean(x, axis=[1, 2], keepdims=True)
        y = tf.nn.relu(self.reduce(y))
        return hard_sigmoid(self.expand(y)) * x

    def get_config(self):
        return {
            **super().get_config(),
            "channels": self.channels,
            "se_ratio": self.se_ratio,
        }


class _ConvBnActivationBlock(keras.layers.Layer):
    def __init__(
        self,
        out_ch: int,
        kernel_size: int,
        stride: int = 1,
        padding: str = "same",
        dilation: int = 1,
        activation: str = "relu",
    ):
        super().__init__()
        self.out_ch = out_ch
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding
        self.dilation = dilation
        self.activation = activation
        self.conv = keras.layers.Conv2D(
            out_ch,
            kernel_size,
            strides=stride,
            padding="same",
            dilation_rate=dilation,
            kernel_initializer=_CONV_INITIALIZER,
            kernel_regularizer=_REGULARIZER,
            use_bias=False,
        )
        self.bn = keras.layers.BatchNormalization()

    def call(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return _activation(x, name=self.activation)

    def get_config(self):
        return {
            **super().get_config(),
            "out_ch": self.out_ch,
            "kernel_size": self.kernel_size,
            "stride": self.stride,
            "padding": self.padding,
            "dilation": self.dilation,
            "activation": self.activation,
        }


class _MobileNetV3Block(keras.layers.Layer):
    def __init__(
        self,
        in_ch: int,
        exp_ch: int,
        out_ch: int,
        kernel_size: int,
        stride: int = 1,
        dilation: int = 1,
        se_ratio: float = None,
        activation="relu",
        allow_residual=True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.in_ch = in_ch
        self.exp_ch = exp_ch
        self.out_ch = out_ch
        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation
        self.se_ratio = se_ratio
        self.activation = activation
        self.allow_residual = allow_residual

        self.apply_residual = allow_residual and (in_ch == out_ch and stride == 1)

        self.layers = []

        if in_ch != exp_ch:
            self.layers += [
                # Pointwise
                keras.layers.Conv2D(
                    exp_ch,
                    1,
                    padding="same",
                    kernel_initializer=_CONV_INITIALIZER,
                    kernel_regularizer=_REGULARIZER,
                    use_bias=False,
                ),
                keras.layers.BatchNormalization(),
                keras.layers.Lambda(lambda x: _activation(x, self.activation)),
            ]

        self.layers += [
            # Depthwise
            keras.layers.DepthwiseConv2D(
                kernel_size,
                strides=stride,
                padding="same",
                dilation_rate=dilation,
                depthwise_initializer=_CONV_INITIALIZER,
                depthwise_regularizer=_REGULARIZER,
                use_bias=False,
            ),
            keras.layers.BatchNormalization(),
            keras.layers.Lambda(lambda x: _activation(x, self.activation)),
        ]
        # SE goes after activation. This is where paper is unclear. In e.g.
        # MNASNet, for instance, SE goes after activation. I've done runs
        # with activation both before and after, and thus far, the results were
        # better with activation before SE. Still not as good as the paper
        # claims, but close enough for practical work.
        if se_ratio is not None:
            self.layers += [_SqueezeAndExcitation(exp_ch, se_ratio)]
        self.layers += [  # Linear pointwise. Note that there's no activation afterwards.
            keras.layers.Conv2D(
                out_ch,
                1,
                padding="same",
                kernel_initializer=_CONV_INITIALIZER,
                kernel_regularizer=_REGULARIZER,
                use_bias=False,
            ),
            keras.layers.BatchNormalization(),
        ]

    def call(self, x):
        input = x
        for layer in self.layers:
            x = layer(x)
        if self.apply_residual:
            x += input
        return x

    def get_config(self):
        return {
            **super().get_config(),
            "in_ch": self.in_ch,
            "exp_ch": self.exp_ch,
            "out_ch": self.out_ch,
            "kernel_size": self.kernel_size,
            "stride": self.stride,
            "dilation": self.dilation,
            "se_ratio": self.se_ratio,
            "activation": self.activation,
            "allow_residual": self.allow_residual,
        }


def create_mobilenet_v3(
    input: keras.Input,
    alpha: float = 1.0,
    num_classes: int = 1000,
    dropout: float = 0.2,  # Paper says 0.8, but they probably mean keep probability.
    model_type: str = "small",
    has_classifier: bool = True,
) -> keras.Model:
    assert alpha > 0.0
    assert num_classes > 1
    assert model_type in conf.CONFIG

    config = conf.CONFIG[model_type]
    # Scale the channels, forcing them to be multiples of 8, biased towards
    # the higher number of channels.
    for c in config:
        c[0] = _round_to_multiple_of(c[0] * alpha, 8)
        c[1] = _round_to_multiple_of(c[1] * alpha, 8)
        c[2] = _round_to_multiple_of(c[2] * alpha, 8)

    # Build the first layer. It's the same for all networks.
    x = _ConvBnActivationBlock(
        config[0][0],
        3,  # kernel_size
        padding=1,
        stride=2,
        dilation=1,
        activation="hardswish",
    )(input)

    # Build the bottleneck stack.
    for idx, c in enumerate(config):
        in_ch, exp_ch, out_ch, kernel_size, stride, dilation, se_ratio, activation = c
        x = _MobileNetV3Block(
            in_ch,
            exp_ch,
            out_ch,
            kernel_size,
            stride,
            se_ratio=se_ratio,
            activation=activation,
            name=f"bottleneck{idx}",
        )(x)

    # Build the classifier.
    shallow_tail = any(x in model_type for x in ["_segmentation", "_detection"])
    if "large" in model_type:
        last_conv_ch = 960 if not shallow_tail else 480
    elif "small" in model_type:
        last_conv_ch = 576 if not shallow_tail else 288
    else:
        raise ValueError("Invalid model type")

    if alpha < 1.0:
        last_conv_ch = _round_to_multiple_of(last_conv_ch * alpha, 8)

    x = _ConvBnActivationBlock(
        last_conv_ch, 1, stride=1, padding="same", dilation=1, activation="hardswish"
    )(x)

    if has_classifier:
        x = keras.layers.GlobalAveragePooling2D()(x)
        x = keras.layers.Flatten()(x)
        x = keras.layers.Dense(
            1280,
            activation=hard_swish,
            kernel_initializer=_CONV_INITIALIZER,
            kernel_regularizer=_REGULARIZER,
            bias_regularizer=_REGULARIZER,
        )(x)
        x = keras.layers.Dropout(dropout)(x)
        output = keras.layers.Dense(
            num_classes,
            kernel_initializer=_CONV_INITIALIZER,
            kernel_regularizer=_REGULARIZER,
            bias_regularizer=_REGULARIZER,
        )(x)
    else:
        output = x

    return keras.Model(inputs=[input], outputs=[output])
