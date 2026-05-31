# TERRA Horizon Project - Use Case 1 Forecaster

**Version**: Alpha 1

This is the Alpha 1 Version of the forecaster for **Use Case 1** of the **TERRA Horizon Project**: *Assessment of water contamination in coastal areas and in the water cycle.*

> **Note:** This version does **not** contain meteorological data (e.g., OpenMeteo). Meteorological integration will be added in future versions.

---

## High-Level Architecture & Features

This pipeline operates purely as an inference engine to forecast water quality indicators.

### Features
We focus on key water quality indicators extracted primarily from satellite imagery (Sentinel-2 and Sentinel-3):
- **Sentinel-2**
    - **CDOM** (Colored Dissolved Organic Matter)
    - **Chl_a** (Chlorophyll-a)
    - **Color** 
    - **Cya** (Cyanobacteria)
    - **DOC** (Dissolved Organic Carbon)
    - **Turb** (Turbidity)
    - **WQI** (Water Quality Index)

- **Sentinel-3**    
    - **Surface Temperature**

### Model Architecture
The core engine is a **Global Multi-Feature Transfer Model**. At a high level, it relies on:
- **BiLSTM (Bidirectional LSTM) Networks**: To capture complex temporal dynamics in both forward and backward time directions.
- **Attention Mechanisms**: To focus the model on the most critical parts of the time series when making predictions.
- **Velocity & Horizon Scaling**: To dynamically scale forecasted changes over multiple steps into the future.
- **Spatial Context Awareness**: The model evaluates bounding box geometry and spatial metrics to generalize across different geographical areas.

---

## Pre-Processing Mechanisms

Before data reaches the model, it goes through several critical pre-processing stages to handle real-world challenges (like cloud cover and satellite revisit gaps):

1. **Water Tile Selection (NDWI)**: Dynamically extracts valid river and water body tiles by analyzing Normalized Difference Water Index (NDWI) distributions.
2. **Matern GPR (Gaussian Process Regression) Interpolation**: Fills gaps in the raw satellite data to produce a clean, continuous **5-day cadence** time series. It uses a Matern kernel to model the natural temporal smoothness of water quality metrics.
3. **Temporal Encoding**: Injects cyclical time features (Sine/Cosine of the Day of the Year and Month) so the model understands seasonal patterns.
4. **Robust Scaling**: Normalizes the features using robust statistical scalars (medians and quantiles) to prevent extreme outlier data from skewing the predictions.

---

## Pipeline Flow

The entire inference process is orchestrated via the central `forecast.py` script. The flow is as follows:

1. **Area Definition**: You define an Area of Interest (AOI bounding box) and a target anchor date.
2. **Tile Extraction**: The pipeline automatically chops the AOI into river tiles.
3. **Validation**: It filters out tiles that lack sufficient water presence.
4. **Data Collection**: It downloads historical Sentinel-2 and Sentinel-3 data for the valid tiles.
5. **Augmentation**: Missing data gaps are interpolated (Matern GPR).
6. **Inference**: The pre-processed 5-day time series is passed to the Global BiLSTM model to forecast the future state of the water quality indicators.
7. **Export**: Predictions are saved as `.json` and `.csv` files, alongside visual plots showing history vs. forecast.

### Example Inference Run

```bash
python forecast.py \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name "sperchios_test_run" \
  --output-root "inference_results"
```

Target-date imagery is requested for the exact anchor date only. If Sentinel-2 or Sentinel-3 imagery is not available on that date, the run records `status: unavailable` and `actual_date: "N/A"` in `inference_plan.json` instead of silently falling back to another date.
