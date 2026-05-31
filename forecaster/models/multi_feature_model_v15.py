from keras import layers, models, initializers, ops as K
from keras.saving import register_keras_serializable
from keras import optimizers, losses, metrics

__name__ = "Single-BiLSTM-Attn-AR-Velocity-V15"

# -------- V15: Horizon-aware learnable velocity shortcut ----------


@register_keras_serializable(package="terra")
class HorizonVelocityScale(layers.Layer):
    """Applies a learnable horizon scale gamma[h] to the velocity shortcut."""

    def __init__(self, horizon: int, **kwargs):
        super().__init__(**kwargs)
        self.horizon = int(horizon)

    def build(self, input_shape):
        init = [[float(step)] for step in range(1, self.horizon + 1)]
        self.gamma = self.add_weight(
            name="gamma",
            shape=(self.horizon, 1),
            initializer=initializers.Constant(init),
            trainable=True,
        )

    def call(self, beta_velocity):
        # beta_velocity: (B, 1, F), gamma: (H, 1) -> (B, H, F)
        gamma = K.reshape(self.gamma, (1, self.horizon, 1))
        return beta_velocity * gamma

    def get_config(self):
        config = super().get_config()
        config.update({"horizon": self.horizon})
        return config


def build_model(
    n_features,
    units1=192,
    units2=125,
    l2=1e-6,
    num_outputs=14,
    use_attention=True,
    loss_name="mse",
    target_indices=None,
    horizon=3,
    hidden: int = 224,
    dropout: float = 0.25,
    clipnorm: float = 1.0,
    huber_delta: float = 1.0,
):
    # Inputs
    x_in = layers.Input(shape=(None, n_features), name="X")  # (B, L, F)

    x1 = layers.LayerNormalization(name="ln1")(x_in)
    x1 = layers.Dropout(dropout, name="pre_rnn1_dropout")(x1)

    # Bi-directional LSTM temporal encoder
    h1 = layers.Bidirectional(
        layers.LSTM(units1, return_sequences=True),
        name="bi_lstm_1",
    )(x1)

    res1 = layers.TimeDistributed(layers.Dense(units1 * 2), name="proj_res1")(x1)
    h1 = layers.Add(name="residual1")([h1, res1])

    # Recency-weighted attention over the encoded sequence
    channels = 2 * units1
    attn_logits = layers.Dense(64, activation="tanh", name="attn_score_mlp")(h1)
    attn_logits = layers.Dense(1, name="attn_score")(attn_logits)
    attn = layers.Softmax(axis=1, name="attn_weights")(attn_logits)
    ctx = layers.Multiply(name="attn_apply")([h1, attn])
    h_attn = layers.Lambda(
        lambda z: K.sum(z, axis=1),
        output_shape=lambda s: (s[0], s[2]),
        name="attn_sum",
    )(ctx)

    h_max = layers.GlobalMaxPooling1D(name="max_pool")(h1)
    rep = layers.Concatenate(name="rep")([h_attn, h_max])
    rep = layers.Dense(channels, activation="gelu", name="mix_attn_max")(rep)

    core = layers.Dense(hidden, activation="gelu")(rep)
    core = layers.Dropout(dropout)(core)
    core = layers.Dense(hidden // 2, activation="gelu")(core)
    core = layers.Dropout(dropout)(core)

    residual = layers.Dense(horizon * num_outputs, name="residual_dense")(core)
    residual = layers.Reshape((horizon, num_outputs), name="residual_out")(residual)

    if target_indices is not None:
        start_idx, end_idx = target_indices

        last_vals = layers.Lambda(
            lambda z: z[:, -1, start_idx:end_idx],
            name="last_targets",
        )(x_in)
        prev_vals = layers.Lambda(
            lambda z: z[:, -2, start_idx:end_idx],
            name="prev_targets",
        )(x_in)

        velocity = layers.Subtract(name="velocity")([last_vals, prev_vals])

        # Feature-dependent gate lets the model suppress velocity when the latest
        # observation/context suggests the recent slope is not reliable.
        last_features = layers.Lambda(lambda z: z[:, -1, :], name="last_features")(x_in)
        gate = layers.Dense(num_outputs, activation="sigmoid", name="velocity_gate")(last_features)
        velocity = layers.Multiply(name="gated_velocity")([velocity, gate])

        alpha_layer = layers.Dense(
            num_outputs,
            use_bias=False,
            kernel_initializer=initializers.Identity(),
            trainable=False,
            name="alpha_scale",
        )
        alpha = alpha_layer(last_vals)
        alpha = layers.Reshape((1, num_outputs), name="last_value_shortcut")(alpha)

        beta = layers.Dense(
            num_outputs,
            use_bias=False,
            kernel_initializer=initializers.Identity(),
            trainable=True,
            name="beta_scale",
        )(velocity)
        beta = layers.Reshape((1, num_outputs), name="beta_velocity")(beta)

        gamma_beta_velocity = HorizonVelocityScale(
            horizon=horizon,
            name="gamma_horizon_velocity",
        )(beta)

        # y_pred[t+h] = residual[h] + y_t + gamma[h] * beta * velocity
        out = layers.Add(name="final_out")([residual, alpha, gamma_beta_velocity])
    else:
        out = residual

    model = models.Model(inputs=[x_in], outputs=out, name=__name__)

    opt = optimizers.AdamW(learning_rate=1e-3, weight_decay=1e-5, clipnorm=clipnorm)
    loss_fn = losses.MeanSquaredError() if loss_name.lower() == "mse" else losses.Huber(delta=huber_delta)

    model.compile(
        optimizer=opt,
        loss=loss_fn,
        metrics=[
            metrics.MeanAbsoluteError(),
            metrics.MeanSquaredError(),
            metrics.RootMeanSquaredError(),
        ],
    )
    return model
