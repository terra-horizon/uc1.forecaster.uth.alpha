from __future__ import annotations

import json
from pathlib import Path

import tensorflow as tf
from keras import initializers, layers, losses, metrics, models, optimizers, ops as K

from forecaster.models import multi_feature_model_v15
from forecaster.models.multi_feature_model_v15 import HorizonVelocityScale
class ForecastWeightedLoss(losses.Loss):
    def call(self, y_true, y_pred): return 0.0
class HorizonWeightedLoss(losses.Loss):
    def call(self, y_true, y_pred): return 0.0


__name__ = "Single-BiLSTM-Attn-AR-Velocity-V15-Global"


def _load_source_model_from_metadata(checkpoint: Path):
    metadata_path = Path(str(checkpoint) + ".metadata.json")
    if not metadata_path.exists():
        raise FileNotFoundError(f"No checkpoint metadata found for fallback warm-start load: {metadata_path}")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    n_features = len(metadata.get("features") or [])
    target_columns = metadata.get("target_columns") or metadata.get("target_cols") or []
    num_outputs = len(target_columns)
    horizon = int(metadata.get("horizon") or 3)
    if n_features <= 0 or num_outputs <= 0:
        raise ValueError(f"Checkpoint metadata lacks feature/target shape information: {metadata_path}")

    source_model = multi_feature_model_v15.build_model(
        n_features=n_features,
        num_outputs=num_outputs,
        target_indices=(0, num_outputs),
        horizon=horizon,
        hidden=256,
        dropout=0.2,
    )
    source_model.load_weights(checkpoint)
    return source_model


def build_model(
    n_features: int,
    num_tiles: int,
    units1: int = 192,
    units2: int = 125,
    l2: float = 1e-6,
    num_outputs: int = 13,
    use_attention: bool = True,
    loss_name: str = "mse",
    target_indices: tuple[int, int] | None = None,
    horizon: int = 3,
    hidden: int = 256,
    dropout: float = 0.2,
    clipnorm: float = 1.0,
    huber_delta: float = 1.0,
    tile_embedding_dim: int = 8,
    use_tile_adapter: bool = True,
    use_spatial_context: bool = False,
    spatial_context_dim: int = 8,
    spatial_embedding_dim: int = 16,
):
    base = multi_feature_model_v15.build_model(
        n_features=n_features,
        units1=units1,
        units2=units2,
        l2=l2,
        num_outputs=num_outputs,
        use_attention=use_attention,
        loss_name=loss_name,
        target_indices=target_indices,
        horizon=horizon,
        hidden=hidden,
        dropout=dropout,
        clipnorm=clipnorm,
        huber_delta=huber_delta,
    )

    if not use_tile_adapter and not use_spatial_context:
        model = models.Model(inputs=base.inputs, outputs=base.output, name=f"{__name__}-Agnostic")
        loss_fn = losses.MeanSquaredError() if loss_name.lower() == "mse" else losses.Huber(delta=huber_delta)
        model.compile(
            optimizer=optimizers.AdamW(learning_rate=3e-4, weight_decay=1e-5, clipnorm=clipnorm),
            loss=loss_fn,
            metrics=[
                metrics.MeanAbsoluteError(),
                metrics.RootMeanSquaredError(),
            ],
        )
        return model

    inputs = [base.inputs[0]]
    adapter_outputs = [base.output]
    name_suffix = []

    if use_spatial_context:
        spatial_context = layers.Input(shape=(int(spatial_context_dim),), dtype="float32", name="spatial_context")
        spatial_embedding = layers.LayerNormalization(name="spatial_context_norm")(spatial_context)
        spatial_embedding = layers.Dense(32, activation="gelu", name="spatial_context_dense_1")(spatial_embedding)
        spatial_embedding = layers.Dense(
            int(spatial_embedding_dim),
            activation="gelu",
            name="spatial_context_dense_2",
        )(spatial_embedding)
        spatial_bias = layers.Dense(
            horizon * num_outputs,
            use_bias=False,
            kernel_initializer="zeros",
            name="spatial_adapter_dense",
        )(spatial_embedding)
        spatial_bias = layers.Reshape((horizon, num_outputs), name="spatial_adapter_out")(spatial_bias)
        inputs.append(spatial_context)
        adapter_outputs.append(spatial_bias)
        name_suffix.append("Spatial")

    if use_tile_adapter:
        tile_id = layers.Input(shape=(), dtype="int32", name="tile_id")
        tile_embedding = layers.Embedding(
            input_dim=max(int(num_tiles), 1),
            output_dim=int(tile_embedding_dim),
            embeddings_initializer=initializers.RandomNormal(stddev=1e-3),
            name="tile_embedding",
        )(tile_id)
        tile_bias = layers.Dense(
            horizon * num_outputs,
            use_bias=False,
            kernel_initializer="zeros",
            name="tile_adapter_dense",
        )(tile_embedding)
        tile_bias = layers.Reshape((horizon, num_outputs), name="tile_adapter_out")(tile_bias)
        inputs.append(tile_id)
        adapter_outputs.append(tile_bias)
        name_suffix.append("Tile")

    output = layers.Add(name="global_final_out")(adapter_outputs)
    model_name = f"{__name__}-{'-'.join(name_suffix)}" if name_suffix else __name__
    model = models.Model(inputs=inputs, outputs=output, name=model_name)
    loss_fn = losses.MeanSquaredError() if loss_name.lower() == "mse" else losses.Huber(delta=huber_delta)
    model.compile(
        optimizer=optimizers.AdamW(learning_rate=3e-4, weight_decay=1e-5, clipnorm=clipnorm),
        loss=loss_fn,
        metrics=[
            metrics.MeanAbsoluteError(),
            metrics.RootMeanSquaredError(),
        ],
    )
    return model


def load_backbone_weights(model, checkpoint_path: str | Path) -> dict:
    checkpoint = Path(checkpoint_path)
    if not checkpoint.exists():
        raise FileNotFoundError(f"Warm-start checkpoint not found: {checkpoint}")

    custom_objects = {
        "tf": tf,
        "K": K,
        "HorizonVelocityScale": HorizonVelocityScale,
        "ForecastWeightedLoss": ForecastWeightedLoss,
        "HorizonWeightedLoss": HorizonWeightedLoss,
    }
    try:
        source_model = tf.keras.models.load_model(
            checkpoint,
            compile=False,
            safe_mode=False,
            custom_objects=custom_objects,
        )
    except Exception as exc:
        print(
            "[WARN] Full checkpoint deserialization failed during warm start; "
            f"falling back to metadata-based weight loading. Reason: {exc}"
        )
        source_model = _load_source_model_from_metadata(checkpoint)

    copied: list[str] = []
    skipped: list[str] = []
    used_source_layers: set[str] = set()
    source_weight_layers = [
        layer
        for layer in source_model.layers
        if layer.get_weights()
    ]

    def matching_source_layer(target_layer):
        try:
            source_layer = source_model.get_layer(target_layer.name)
            if source_layer.name not in used_source_layers:
                return source_layer
        except ValueError:
            pass

        target_weights = target_layer.get_weights()
        for source_layer in source_weight_layers:
            if source_layer.name in used_source_layers:
                continue
            source_weights = source_layer.get_weights()
            if len(source_weights) != len(target_weights):
                continue
            if all(sw.shape == tw.shape for sw, tw in zip(source_weights, target_weights)):
                return source_layer
        return None

    adapter_layer_names = {
        "tile_id",
        "tile_embedding",
        "tile_adapter_dense",
        "tile_adapter_out",
        "spatial_context",
        "spatial_context_norm",
        "spatial_context_dense_1",
        "spatial_context_dense_2",
        "spatial_adapter_dense",
        "spatial_adapter_out",
        "global_final_out",
    }

    for layer in model.layers:
        if layer.name in adapter_layer_names:
            continue

        target_weights = layer.get_weights()
        if not target_weights:
            continue

        source_layer = matching_source_layer(layer)
        if source_layer is None:
            skipped.append(layer.name)
            continue

        source_weights = source_layer.get_weights()
        if len(source_weights) != len(target_weights):
            skipped.append(layer.name)
            continue
        if any(sw.shape != tw.shape for sw, tw in zip(source_weights, target_weights)):
            skipped.append(layer.name)
            continue
        layer.set_weights(source_weights)
        used_source_layers.add(source_layer.name)
        copied.append(layer.name if source_layer.name == layer.name else f"{source_layer.name}->{layer.name}")

    return {
        "checkpoint": str(checkpoint),
        "copied_layers": copied,
        "skipped_layers": skipped,
        "copied_count": len(copied),
        "skipped_count": len(skipped),
    }


def set_backbone_trainable(model, trainable: bool) -> None:
    adapter_layers = {
        "tile_embedding",
        "tile_adapter_dense",
        "tile_adapter_out",
        "spatial_context_norm",
        "spatial_context_dense_1",
        "spatial_context_dense_2",
        "spatial_adapter_dense",
        "spatial_adapter_out",
        "global_final_out",
    }
    input_layers = {"tile_id", "spatial_context"}
    for layer in model.layers:
        if layer.name in adapter_layers:
            layer.trainable = True
        elif layer.name not in input_layers:
            layer.trainable = bool(trainable)
