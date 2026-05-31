import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern, WhiteKernel, ConstantKernel
from sklearn.preprocessing import StandardScaler

class DataAugmentation:
    """
    Data Augmentation pipeline utilizing GPR Matern interpolation for a realistic
    simulation of continuous environmental variables with correlated noise.
    - Reads the source CSV.
    - Drops 'WQI' feature if present.
    - Interpolates missing rows on a 5-day grid using Matern GPR.
    - Saves the interpolated table and produces comparison plots.
    """

    def __init__(
        self,
        input_path="data/csv/mean_metrics.csv",
        output_path="data/csv/5D_mean_metrics_interpolated_time_based.csv",
        summary_plot_path="data/plots/interpolation/all_features_5D_interpolation.png",
        per_feature_dir="data/plots/interpolation/plots/",
        freq="5D",
    ):
        self.input_path = input_path
        self.output_path = output_path
        self.summary_plot_path = summary_plot_path
        self.per_feature_dir = per_feature_dir
        self.freq = freq

    def remove_outliers(self, df):
        """
        Removes outliers using the Interquartile Range (IQR) method.
        Generic 1.5 * IQR rule.
        """
        print("\n[Outlier Removal] checks...")
        df_cleaned = df.copy()
        
        # Only apply to numeric columns
        numeric_cols = df_cleaned.select_dtypes(include=['float64', 'float32', 'int64', 'int32']).columns
        
        for col in numeric_cols:
            Q1 = df_cleaned[col].quantile(0.25)
            Q3 = df_cleaned[col].quantile(0.75)
            IQR = Q3 - Q1
            
            lower_bound = Q1 - 1.5 * IQR
            upper_bound = Q3 + 1.5 * IQR
            
            outliers = (df_cleaned[col] < lower_bound) | (df_cleaned[col] > upper_bound)
            num_outliers = outliers.sum()
            
            if num_outliers > 0:
                print(f"  - {col}: Removed {num_outliers} outliers (Bounds: [{lower_bound:.4f}, {upper_bound:.4f}])")
                df_cleaned.loc[outliers, col] = float('nan')
                
        return df_cleaned

    @staticmethod
    def fit_matern_gpr(X_train, y_train, X_full):
        scaler_X = StandardScaler()
        scaler_y = StandardScaler()
        
        X_train_reshaped = X_train.reshape(-1, 1)
        y_train_reshaped = y_train.reshape(-1, 1)
        X_full_reshaped = X_full.reshape(-1, 1)

        X_train_scaled = scaler_X.fit_transform(X_train_reshaped)
        y_train_scaled = scaler_y.fit_transform(y_train_reshaped).flatten()
        X_full_scaled = scaler_X.transform(X_full_reshaped)
        
        # Kernel: Matern + WhiteKernel
        kernel = ConstantKernel(1.0) * Matern(length_scale=1.0, nu=0.5) + \
                 WhiteKernel(noise_level=1e-5)
                 
        gpr = GaussianProcessRegressor(kernel=kernel, n_restarts_optimizer=10, normalize_y=False)
        gpr.fit(X_train_scaled, y_train_scaled)
        
        # Predict Mean and Std
        y_mean_scaled, y_std_scaled = gpr.predict(X_full_scaled, return_std=True)
        
        # Sample ONE realistic path from the posterior
        y_samples_scaled = gpr.sample_y(X_full_scaled, n_samples=1, random_state=42).flatten()

        # Inverse transform
        y_mean = scaler_y.inverse_transform(y_mean_scaled.reshape(-1, 1)).flatten()
        y_sample_raw = scaler_y.inverse_transform(y_samples_scaled.reshape(-1, 1)).flatten()
        y_std = y_std_scaled * scaler_y.scale_[0]

        # Constrain and Anchor the Noise
        noise_component = y_sample_raw - y_mean
        y_final_sample = y_mean + 0.50 * noise_component

        # Strictly Anchor known data points
        known_values = dict(zip(X_train.flatten(), y_train.flatten()))
        for i, day in enumerate(X_full.flatten()):
            if day in known_values:
                y_final_sample[i] = known_values[day]

        return y_mean, y_final_sample, y_std

    def run(self):
        df = pd.read_csv(self.input_path)
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
        df = df.sort_values("date").set_index("date")
        
        # Ensure index has no duplicate dates before reindexing
        df = df[~df.index.duplicated(keep='first')]

        # Include WQI in the features now that it is fixed
        if 'WQI' in df.columns:
            print("WQI feature is present and will be included in the interpolation pipeline.")

        print(df.info())
        print(df.head())

        # --- Remove Outliers (IQR Method) ---
        df = self.remove_outliers(df)
        
        full_index = pd.date_range(start=df.index.min(), end=df.index.max(), freq=self.freq)
        start_date = df.index.min()
        full_days = (full_index - start_date).days.values
        
        df_reindexed = df.reindex(full_index)
        
        # We will create df_interpolated dynamically
        df_interpolated = pd.DataFrame(index=full_index)
        
        os.makedirs(self.per_feature_dir, exist_ok=True)
        
        num_cols = len(df.columns)
        rows = int(np.ceil(num_cols / 2))
        plt.figure(figsize=(14, max(4, 3 * rows)))
        
        # Iterate over columns and fit GPR
        for i, col in enumerate(df.columns, 1):
            series = df[col].dropna()
            
            # If no valid data, skip or just fill with 0
            if series.empty:
                print(f"Warning: Column {col} is empty, filling with 0.")
                df_interpolated[col] = 0
                continue
                
            days_since_start = (series.index - start_date).days.values
            values = series.values
            
            print(f"Running GPR Matern for column: {col}...")
            try:
                y_mean, y_final_sample, y_std = self.fit_matern_gpr(days_since_start, values, full_days)
                df_interpolated[col] = y_final_sample
                df_interpolated[f"{col}_gpr_std"] = y_std
            except Exception as e:
                print(f"GPR Matern failed for {col}: {e}")
                raise e
                
            # Plotting on the summary plot
            plt.subplot(rows if rows > 0 else 1, 2, i)
            plt.plot(df.index, df[col], "o", label="Original", alpha=0.6, color="black", markersize=4)
            plt.plot(
                df_interpolated.index,
                df_interpolated[col],
                "-",
                label="Matern GPR (Sample)",
                linewidth=1.5,
                color="purple",
                alpha=0.9
            )
            plt.plot(
                df_interpolated.index,
                pd.Series(y_mean, index=full_index),
                "-",
                label="Mean",
                linewidth=1,
                color="blue",
                alpha=0.4
            )
            plt.title(col)
            plt.xlabel("Date")
            plt.ylabel(col)
            plt.legend()
            plt.grid(True, linestyle="--", alpha=0.5)

            # Per-feature plots
            fig, axes = plt.subplots(1, 2, figsize=(14, 4), sharey=True)
            fig.suptitle(f"{col} — Before and After Matern GPR Interpolation", fontsize=14, fontweight="bold")

            axes[0].plot(df.index, df[col], "o-", color="black", alpha=0.7)
            axes[0].set_title("Before Interpolation")
            axes[0].set_xlabel("Date")
            axes[0].set_ylabel(col)
            axes[0].grid(True, linestyle="--", alpha=0.5)

            axes[1].plot(df_interpolated.index, df_interpolated[col], "-", color="purple", alpha=0.9, label='Sample')
            axes[1].plot(df_interpolated.index, y_mean, "-", color="blue", alpha=0.4, label='Mean')
            axes[1].scatter(df.index[df[col].notna()], df[col].dropna(), color="black", zorder=10, label="Original Data", s=15)
            axes[1].set_title("After Matern GPR Interpolation")
            axes[1].set_xlabel("Date")
            axes[1].legend()
            axes[1].grid(True, linestyle="--", alpha=0.5)

            plt.tight_layout()
            plt.savefig(os.path.join(self.per_feature_dir, f"{col}_matern_gpr_interpolation.png"))
            plt.close(fig)

        plt.tight_layout()
        plt.savefig(self.summary_plot_path)
        plt.close()

        missing_before = df_reindexed.isna().sum()
        missing_after = df_interpolated.isna().sum()

        print("Missing values before interpolation:")
        print(missing_before)
        print("Missing values after interpolation:")
        print(missing_after)

        print(df_interpolated.head(10))

        df_final = df_interpolated.copy()
        df_final.index = df_final.index.strftime("%d-%m-%Y")
        df_final = df_final.reset_index().rename(columns={"index": "date"})
        
        # Ensure output directory exists before saving
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        df_final.to_csv(self.output_path, index=False)

        return self.output_path
