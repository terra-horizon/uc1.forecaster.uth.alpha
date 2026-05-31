import requests
import json
import time
import os
import math
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
import pandas as pd
import matplotlib.pyplot as plt
from config import CDSE_Credentials as CDSE_Credentials
from .. import evalscripts as scripts

DATA_DIR = "forecaster/data"
RETRY_DELAY_SECONDS = 180  # 3 minutes


@dataclass(frozen=True)
class ImageDataSource:
    collection_type: str
    alias: str | None = None
    mosaicking_order: str | None = "leastCC"
    max_cloud_coverage: int | None = None


@dataclass(frozen=True)
class ImageProduct:
    key: str
    evalscript: str
    collection_label: str
    data_sources: tuple[ImageDataSource, ...]
    output_format: str = "image/png"
    file_extension: str = "png"

def _sleep_with_countdown(seconds: int, message: str) -> None:
    """Colored countdown logger to highlight rate-limit waits."""
    print(f"\033[93m{message}\033[0m")
    for remaining in range(seconds, 0, -1):
        print(
            f"\r\033[93m[WAIT] Retrying in {remaining:3d}s...\033[0m",
            end="",
            flush=True,
        )
        time.sleep(1)
    print()

class StatisticalCollection:
    def __init__(self, time_interval=None, bbox=[], dir=DATA_DIR, max_cloud_coverage=30):
        # API Authentication Credentials
        self.token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
        self.credential_sets = CDSE_Credentials.get_credential_sets()
        self.credential_index = 0
        self.client_id = self.credential_sets[0]["client_id"]
        self.client_secret = self.credential_sets[0]["client_secret"]
        self.api_url = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
        self.access_token = self.get_access_token()
        self.bbox = bbox
        self.dir = dir
        self.max_cloud_coverage = max_cloud_coverage

        # Evalscript (water masking)
        self.evalscripts = {
            "Se2WaQ": scripts.wqi,    
            "Se2WaQ2": scripts.wqi2,    
        }
        self.chl_a_thresholds = [
            (0, 100), (5, 100), (10, 80), (20, 60), (40, 30), (1000, 0)
        ]
        self.cya_thresholds = [
            (0, 100), (20000, 80), (100000, 20), (1000000, 0)
        ]
        self.cdom_thresholds = [
            (0, 100), (5, 80), (10, 60), (20, 40), (50, 20), (100, 0)
        ]
        self.turbidity_thresholds = [   
            (0, 100), (1, 100), (5, 80), (20, 20), (100, 0)
        ]
        self.doc_thresholds = [
            (0, 100), (4, 100), (10, 60), (30, 0)
        ]
        self.color_thresholds = [
            (0, 100), (15, 90), (50, 40), (200, 0)
        ]
        # Define Time Intervals
        self.slots = []

        if time_interval:
            # time_interval is (start_str, end_str) e.g. ("2020-01-01", "2025-12-31")
            s_str, e_str = time_interval
            # Parse dates to handle yearly splitting
            try:
                # Handle possible ISO format with T/Z by taking just first 10 chars for date
                s_date = datetime.strptime(s_str[:10], "%Y-%m-%d").date()
                e_date = datetime.strptime(e_str[:10], "%Y-%m-%d").date()
                
                for year in range(s_date.year, e_date.year + 1):
                    # Define year start/end
                    year_start = date(year, 1, 1)
                    year_end = date(year, 12, 31)
                    
                    # Clip to actual interval
                    current_start = max(s_date, year_start)
                    current_end = min(e_date, year_end)
                    
                    self.slots.append([current_start.isoformat(), current_end.isoformat()])
            except ValueError as e:
                print(f"Error parsing time_interval dates: {e}. Falling back to default years.")
                
        if not self.slots: # Fallback or if time_interval not provided
            current_year = date.today().year
            for year in range(current_year - 1, current_year + 1):
                start = date(year, 1, 1).isoformat()
                end = date(year, 12, 31).isoformat()
                self.slots.append([start, end])

    @staticmethod
    def _redact_client_id(client_id: str) -> str:
        if len(client_id) <= 10:
            return "***"
        return f"{client_id[:8]}...{client_id[-4:]}"

    def _current_credential_label(self):
        return self.credential_sets[self.credential_index].get("label", "credentials")

    def _switch_to_next_credentials(self, status_code: int) -> bool:
        if self.credential_index + 1 >= len(self.credential_sets):
            return False
        current = self.credential_sets[self.credential_index]
        self.credential_index += 1
        next_credentials = self.credential_sets[self.credential_index]
        self.client_id = next_credentials["client_id"]
        self.client_secret = next_credentials["client_secret"]
        self.access_token = self.get_access_token()
        print(
            f"[Statistics] Received {status_code} using "
            f"{current.get('label', 'credentials')} credentials; switching to "
            f"{next_credentials.get('label', 'credentials')} "
            f"({self._redact_client_id(self.client_id)})."
        )
        return bool(self.access_token)

    def get_access_token(self):
        """Retrieve an access token from Copernicus Data Space Ecosystem"""
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }

        try:
            response = requests.post(self.token_url, data=payload, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"Failed to retrieve access token: {e}")
            return None

        if response.status_code == 200:
            token_data = response.json()
            print(f"Access token retrieved successfully with {self._current_credential_label()} credentials ({self._redact_client_id(self.client_id)}).")
            return token_data["access_token"]
        else:
            print(f"Failed to retrieve access token: {response.status_code}")
            print("Error Message:", response.text)
            return None

    def get_request(self, evalscript, time_interval, bbox, retries: int = 3):
        """Send request to Copernicus API and retrieve statistics with retry on 429/403."""
        if not self.access_token:
            print("No access token available. Cannot proceed.")
            return None

        payload = {
            "input": {
                "bounds": {
                    "bbox": bbox,  # Sperchios
                },
                "data": [{
                    "type": "sentinel-2-l2a",
                    "dataFilter": {
                        "timeRange": {
                            "from": f"{time_interval[0]}T00:00:00Z",  #  FIXED TIME FORMAT
                            "to": f"{time_interval[1]}T23:59:59Z"
                        },
                        "maxCloudCoverage": self.max_cloud_coverage,
                    },
                }]
            },
            "aggregation": {
                "timeRange": {
                    "from": f"{time_interval[0]}T00:00:00Z",
                    "to": f"{time_interval[1]}T23:59:59Z"
                },
                "aggregationInterval": {"of": "P1D"},  
                "evalscript": evalscript
            },
        }

        response = None
        for attempt in range(1, retries + 1):
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
            }

            response = requests.post(self.api_url, headers=headers, json=payload)

            if response.status_code == 200:
                print(f"API Response: {response.status_code}")
                return response

            if response.status_code == 401:
                print("[Statistics] Token expired, refreshing...")
                self.access_token = self.get_access_token()
                continue

            if response.status_code in (403, 429):
                if self._switch_to_next_credentials(response.status_code):
                    continue
                if attempt < retries:
                    _sleep_with_countdown(
                        RETRY_DELAY_SECONDS,
                        f"[Statistics] Rate limited ({response.status_code}). Waiting 3 minutes before retry {attempt + 1}/{retries}...",
                    )
                    continue
                print(f"[Statistics] Rate limit persisted after {retries} attempts.")
                return response

            print(f"API Response: {response.status_code}")
            return response

        return response

    def save_data(self):
        """Retrieve and save statistical data for defined time slots"""
        image_count = 1
        if not os.path.exists(self.dir+'/statistical/'): 
            os.makedirs(self.dir+"/statistical/") 

        for slot in self.slots:
            image_count += 1
            # response = self.get_request(self.evalscripts['Se2WaQ2'],slot,bbox)
            response = self.get_request(self.evalscripts['Se2WaQ2'],slot, self.bbox)
            if not response:
                print("[Statistics] No response received; skipping this slot.")
                continue
            with open('response', 'w') as json_file:
                    json.dump(response.json(), json_file, indent=4)
            # print(f"Data saved")
            if response and response.status_code == 200:
                values=self.compute_values(response.json())
                filename = f'{self.dir}/statistical/{" - ".join(slot)}_{image_count}.json'
                with open(filename, 'w') as json_file:
                    json.dump(values, json_file, indent=4)
                print(f"Data saved to {filename}")

    def normalize(self,value,thresholds):
        """
        Normalize a value based on threshold-score pairs.
        thresholds: list of tuples (threshold_value, score), ordered by increasing threshold_value
        """
        for i in range(len(thresholds) - 1):
            x1, s1 = thresholds[i]
            x2, s2 = thresholds[i + 1]
            if x1 <= value <= x2:
                # Linear interpolation
                return s1 + ((value - x1) / (x2 - x1)) * (s2 - s1)
        # Out of bounds
        if value < thresholds[0][0]:
            return thresholds[0][1]
        if value > thresholds[-1][0]:
            return thresholds[-1][1]
            
        
    def compute_values(self,response_json):
        results=[]

        types=['min','max','mean','stDev']
        for interval in response_json["data"]:
            date_str = interval["interval"]["from"][:10]  # Extract date in yyyy-mm-dd format
            date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y")  # Convert to dd-mm-yyyy
            # bands = interval["outputs"]["data"]["bands"]
            outputs = interval.get("outputs", {})
            data = outputs.get("data", {})
            bands = data.get("bands", {})
            results_mean = []
            results_min = []
            results_max = []
            results_stdev = []

            # print(bands)
            for i in types:
         # Extract band values safely
                B01 = bands.get("B0", {}).get("stats", {}).get(i, None)  # Aerosol
                B02 = bands.get("B1", {}).get("stats", {}).get(i, None)  # Blue
                B03 = bands.get("B2", {}).get("stats", {}).get(i, None)  # Green
                B04 = bands.get("B3", {}).get("stats", {}).get(i, None)  # Red
                B05 = bands.get("B4", {}).get("stats", {}).get(i, None)  
                # B05 = bands.get("B5", {}).get("stats", {}).get(i, None)  
                # B06 = bands.get("B6", {}).get("stats", {}).get(i, None)  
                # B07 = bands.get("B7", {}).get("stats", {}).get(i, None)  
                # B08 = bands.get("B8", {}).get("stats", {}).get(i, None)  
                # B09 = bands.get("B9", {}).get("stats", {}).get(i, None)  
                # B10 = bands.get("B10", {}).get("stats", {}).get(i, None)  
                # B11 = bands.get("B11", {}).get("stats", {}).get(i, None) 
                # B12 = bands.get("B12", {}).get("stats", {}).get(i, None) 


                # Handle NaN values
                if any(val in [None, "NaN", float("NaN")] or val is None  for val in [B01, B02, B03, B04,B05]):  # Check for missing values
                    continue

                # Convert to float
                B01, B02, B03, B04,B05 = map(float, [B01, B02, B03, B04,B05])
                # Calculate the indices
                if 0.0 in [B01, B02, B03, B04,B05]:  
                    Chl_a=Cya=Turb=CDOM=DOC=Color=WQI=0.0
                    entry = {
                    "Chl_a": Chl_a,
                    "Cya": Cya,
                    "Turb": Turb,
                    "CDOM": CDOM,
                    "DOC": DOC,
                    "Color": Color,
                    "WQI":WQI,
                }
                else:
                    # Chlorophyll-a (Chl_a) calculation in mg/m^3 = μg/l  0-15
                    Chl_a = 4.26 * (B03 / B01) ** 3.94

                    # Cyanobacteria Index (Cya) in 10^3 cells/ml
                    Cya = 115530.31 * (B03 * B04 / B02) ** 2.38
                    
                    # Turbidity (Turb) in NTU  0-1000
                    Turb = 8.93 * (B03 / B01) - 6.39

                    # Colored Dissolved Organic Matter (CDOM) in mg/l
                    CDOM = 537 * math.exp(-2.93 * B03 / B04)

                    # Dissolved Organic Carbon (DOC) in mg/l   0.1 - 115   
                    DOC = 432 * math.exp(-2.24 * B03 / B04)

                    # Color Index (Color) in mg.Pt/l TCU
                    Color = 25366 * math.exp(-4.53 * B03 / B04)

                    WQI = ((B02+(B01-B03))-B05)/((B02+(B01-B03))+B05)
                    entry = {
                        "Chl_a": Chl_a,
                        "Cya": Cya,
                        "Turb": Turb,
                        "CDOM": CDOM,
                        "DOC": DOC,
                        "Color": Color,
                        "WQI":WQI,
                    }
                # Store results
                match i:
                    case 'min':
                        results_min.append(entry)
                    case 'max':
                        results_max.append(entry)
                    case 'mean':
                        results_mean.append(entry)
                    case 'stDev':
                        results_stdev.append(entry)

            if results_min and results_max and results_mean and results_stdev:
                results.append({
                    date: {
                        "min": results_min[0] ,
                        "max": results_max[0] ,
                        "mean": results_mean[0] ,
                        "stDev": results_stdev[0] ,
                    }
                })

        return results
    

    def make_csv_per_stat(self, json_folder, output_folder):
        os.makedirs(output_folder, exist_ok=True)
        data = []

        # Read all JSON files from each coordinate folder
        # for folder in os.listdir(json_folder):
        #     folder_path = os.path.join(json_folder, folder)
            
        #     if not os.path.isdir(folder_path):
        #         continue  # skip files if any

        #     for json_file in os.listdir(folder_path):
        #         if json_file.endswith('.json'):
        #             filepath = os.path.join(folder_path, json_file)
        #             with open(filepath, 'r') as f:
        #                 content = json.load(f)
        #                 data.extend(content)  # assuming each JSON is a list
        for filename in os.listdir(json_folder):
            if filename.endswith('.json'):
                filepath = os.path.join(json_folder, filename)
                with open(filepath, 'r') as f:
                    content = json.load(f)
                    data.extend(content)  # assuming each JSON is a list

        # Collect all available elements
        elements = set()
        for record in data:
            for date, values in record.items():
                for stat_type in ["min", "max", "mean", "stDev"]:
                    if values[stat_type]:
                        elements.update(values[stat_type].keys())

        elements = sorted(elements)  # sort for nice column order

        # Initialize data containers per stat_type
        stat_data = {stat_type: [] for stat_type in ["min", "max", "mean", "stDev"]}

        # Fill the data
        for record in data:
            for date, values in record.items():
                for stat_type in ["min", "max", "mean", "stDev"]:
                    row = {"date": date}
                    for el in elements:
                        if values[stat_type] and el in values[stat_type]:
                            row[el] = values[stat_type][el]
                        else:
                            row[el] = 0.0  # default 0 if missing
                    stat_data[stat_type].append(row)

        # Write one CSV per stat_type
        for stat_type, rows in stat_data.items():
            filename = os.path.join(output_folder, f"{stat_type}_metrics.csv")
            with open(filename, mode="w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["date"] + elements)
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)

        print(f"All CSV files (min, max, mean, stDev) saved inside '{output_folder}' folder!")

    def run(self, json_output_folder=DATA_DIR+"/statistical", csv_output_folder=DATA_DIR+"/csv"):
        """
        Convenience wrapper that mirrors the original wrapper script:
        1. Fetch and persist raw statistics as JSON.
        2. Materialize per-stat CSVs.
        """
        self.save_data()
        self.make_csv_per_stat(json_folder=json_output_folder, output_folder=csv_output_folder)

class ImageCollection:
    PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
    TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

    def __init__(
        self,
        bbox,
        dir=DATA_DIR,
        tile_name="tile",
        tile_size=400,
        time_from="2024-12-01T00:00:00Z",
        time_to="2025-03-01T23:59:59Z",
        data_collection="sentinel-2-l2a",
        mosaicking_order="leastCC",
        crs="http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        upsampling="BILINEAR",
        downsampling="BILINEAR",
        output_format="image/png",
        file_extension="png",
    ):
        """
        Image collection for Sentinel-2 tiles using evalscripts.

        Args:
            bbox (list[float]): [min_lon, min_lat, max_lon, max_lat].
            box_size: Optional tile size metadata (stored, not used in request).
            dir (str): Base output directory.
            tile_name (str): Logical name of tile (used in filenames).
            resolution_m (float): Target resolution in meters.
            time_from (str): ISO date-time string (UTC).
            time_to (str): ISO date-time string (UTC).
            data_collection (str): e.g. 'sentinel-2-l2a'.
            mosaicking_order (str): e.g. 'leastCC'.
            crs (str): CRS URL.
            upsampling (str): Upsampling method.
            downsampling (str): Downsampling method.
            output_format (str): e.g. 'image/png', 'image/tiff'.
            file_extension (str): File extension to save, e.g. 'png', 'tif'.
        """
        # Auth
        self.credential_sets = CDSE_Credentials.get_credential_sets()
        self.credential_index = 0
        self.client_id = self.credential_sets[self.credential_index]["client_id"]
        self.client_secret = self.credential_sets[self.credential_index]["client_secret"]
        self.access_token = self.get_access_token()

        # Geometry / resolution
        self.bbox = bbox
        print(f"[ImageCollection] BBOX: {self.bbox}")
        self.tile_size = tile_size
        # self.resx_deg, self.resy_deg = self.resolution_to_degrees(resolution_m, bbox)
        self.resx_deg, self.resy_deg = self.calculate_resolution_from_bbox(bbox, tile_size, tile_size)
        print(f"[ImageCollection] Target resolution: {self.resx_deg:.2f} m/px x {self.resy_deg:.2f} m/px")
        
        # Processing config
        self.time_from = time_from
        self.time_to = time_to
        self.data_collection = data_collection
        self.mosaicking_order = mosaicking_order
        self.crs = crs
        self.upsampling = upsampling
        self.downsampling = downsampling
        self.output_format = output_format
        self.file_extension = file_extension

        # Output
        self.base_dir = dir
        self.tile_name = tile_name
        self.output_dir = os.path.join(self.base_dir, "tiles")
        os.makedirs(self.output_dir, exist_ok=True)

        self.products = self._default_products()

    # ---------- auth ----------

    @staticmethod
    def _redact_client_id(client_id: str) -> str:
        if len(client_id) <= 10:
            return "***"
        return f"{client_id[:8]}...{client_id[-4:]}"

    def _current_credential_label(self):
        return self.credential_sets[self.credential_index].get("label", "credentials")

    def _switch_to_next_credentials(self, status_code: int) -> bool:
        if self.credential_index + 1 >= len(self.credential_sets):
            return False
        current = self.credential_sets[self.credential_index]
        self.credential_index += 1
        next_credentials = self.credential_sets[self.credential_index]
        self.client_id = next_credentials["client_id"]
        self.client_secret = next_credentials["client_secret"]
        self.access_token = self.get_access_token()
        print(
            f"[ImageCollection] Received {status_code} using "
            f"{current.get('label', 'credentials')} credentials; switching to "
            f"{next_credentials.get('label', 'credentials')} "
            f"({self._redact_client_id(self.client_id)})."
        )
        return bool(self.access_token)

    def get_access_token(self):
        """Retrieve an access token from Copernicus Data Space Ecosystem."""
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            response = requests.post(self.TOKEN_URL, data=payload, headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"[ImageCollection] Failed to retrieve access token: {e}")
            return None

        if response.status_code == 200:
            token_data = response.json()
            print(
                f"[ImageCollection] Access token retrieved successfully with "
                f"{self._current_credential_label()} credentials "
                f"({self._redact_client_id(self.client_id)})."
            )
            return token_data["access_token"]
        else:
            print(f"[ImageCollection] Failed to retrieve access token: {response.status_code}")
            print("Error Message:", response.text)
            return None

    # ---------- helpers ----------

    @staticmethod
    def resolution_to_degrees(resolution, bbox):
        """
        Converts resolution in meters to degrees.

        Args:
            resolution (float): The resolution in meters.
            bbox (list): [min_lon, min_lat, max_lon, max_lat].

        Returns:
            tuple: (resx_deg, resy_deg).
        """
        # Approx meters per degree latitude
        resy_deg = resolution / 111_320.0

        # For longitude, adjust with latitude
        lat_rad = math.radians(bbox[1])  # min latitude
        resx_deg = resolution / (111_320.0 * math.cos(lat_rad))

        return resx_deg, resy_deg

    @staticmethod
    def calculate_resolution_from_bbox(bbox, width_px, height_px):
        """
        Calculates the resolution in meters/pixel given a bounding box and desired pixel dimensions.
        
        Args:
            bbox (list): [min_lon, min_lat, max_lon, max_lat].
            width_px (int): Desired width in pixels.
            height_px (int): Desired height in pixels.
            
        Returns:
            float: Estimated resolution in meters per pixel.
        """
        min_lon, min_lat, max_lon, max_lat = bbox
        
        # Calculate physical dimensions in meters
        # Height (latitude difference)
        lat_diff = max_lat - min_lat
        height_m = lat_diff * 111320.0
        
        # Width (longitude difference) - use average latitude for better accuracy
        lon_diff = max_lon - min_lon
        avg_lat_rad = math.radians((min_lat + max_lat) / 2)
        width_m = lon_diff * 111320.0 * math.cos(avg_lat_rad)
        
        # Calculate resolution for both dimensions
        res_x = width_m / width_px
        res_y = height_m / height_px
        
        return res_x, res_y

    @staticmethod
    def _default_products() -> dict[str, ImageProduct]:
        sentinel2_source = ImageDataSource(collection_type="sentinel-2-l2a", mosaicking_order="leastCC")
        return {
            "true_color": ImageProduct(
                key="true_color",
                evalscript=scripts.true_color_optimized,
                collection_label="sentinel-2-l2a",
                data_sources=(sentinel2_source,),
            ),
            "chla": ImageProduct(
                key="chla",
                evalscript=scripts.chl_a,
                collection_label="sentinel-2-l2a",
                data_sources=(sentinel2_source,),
            ),
            "cdom": ImageProduct(
                key="cdom",
                evalscript=scripts.cdom,
                collection_label="sentinel-2-l2a",
                data_sources=(sentinel2_source,),
            ),
            "turb": ImageProduct(
                key="turb",
                evalscript=scripts.turb,
                collection_label="sentinel-2-l2a",
                data_sources=(sentinel2_source,),
            ),
            "doc": ImageProduct(
                key="doc",
                evalscript=scripts.doc,
                collection_label="sentinel-2-l2a",
                data_sources=(sentinel2_source,),
            ),
            "cya": ImageProduct(
                key="cya",
                evalscript=scripts.cya,
                collection_label="sentinel-2-l2a",
                data_sources=(sentinel2_source,),
            ),
            "surface_temperature": ImageProduct(
                key="surface_temperature",
                evalscript=scripts.surface_temperature,
                collection_label="sentinel-3-slstr+sentinel-3-olci",
                data_sources=(
                    ImageDataSource(
                        collection_type="sentinel-3-slstr",
                        alias="S3SLSTR",
                        mosaicking_order=None,
                        max_cloud_coverage=100,
                    ),
                    ImageDataSource(
                        collection_type="sentinel-3-olci",
                        alias="S3OLCI",
                        mosaicking_order=None,
                        max_cloud_coverage=100,
                    ),
                ),
            ),
        }

    @classmethod
    def supported_keys(cls) -> tuple[str, ...]:
        return tuple(cls._default_products().keys())

    @classmethod
    def collection_label_for_key(cls, key: str) -> str:
        product = cls._default_products().get(key)
        return product.collection_label if product else "unknown"

    def _product_for_key(self, key: str) -> ImageProduct:
        products = getattr(self, "products", None) or self._default_products()
        if key not in products:
            supported = ", ".join(products)
            raise KeyError(f"[ImageCollection] Evalscript key '{key}' not found. Supported image keys: {supported}.")
        return products[key]

    def _build_data_entry(self, source: ImageDataSource) -> dict[str, Any]:
        data_filter = {
            "timeRange": {
                "from": self.time_from,
                "to": self.time_to,
            }
        }
        if source.mosaicking_order:
            data_filter["mosaickingOrder"] = source.mosaicking_order
        if source.max_cloud_coverage is not None:
            data_filter["maxCloudCoverage"] = source.max_cloud_coverage

        entry: dict[str, Any] = {
            "type": source.collection_type,
            "dataFilter": data_filter,
            "processing": {
                "upsampling": self.upsampling,
                "downsampling": self.downsampling,
            },
        }
        if source.alias:
            entry["id"] = source.alias
        return entry

    def _build_payload(self, product: ImageProduct | str):
        """Build process request payload for a product."""
        if isinstance(product, str):
            product = self._product_for_key(product)
        return {
            "input": {
                "bounds": {
                    "properties": {"crs": self.crs},
                    "bbox": self.bbox,
                },
                "data": [self._build_data_entry(source) for source in product.data_sources],
            },
            "output": {
                # "resx": self.resx_deg,
                # "resy": self.resy_deg,
                "width": self.tile_size,
                "height": self.tile_size,
                "responses": [
                    {
                        "identifier": "default",
                        "format": {"type": product.output_format},
                    }
                ],
            },
            "evalscript": product.evalscript,
        }

    def _post(self, payload, retries: int = 3):
        """POST to /process with token refresh on failure and waits on rate limiting."""
        if not self.access_token:
            print("[ImageCollection] No access token available. Cannot proceed.")
            return None

        response = None
        for attempt in range(1, retries + 1):
            headers = {
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Authorization": f"Bearer {self.access_token}",
            }

            print(f"[ImageCollection] Sending request to {self.PROCESS_URL}...")
            try:
                response = requests.post(self.PROCESS_URL, headers=headers, json=payload, timeout=60)
            except requests.exceptions.Timeout:
                print(f"[ImageCollection] Request timed out after 60s.")
                return None
            except requests.exceptions.RequestException as e:
                print(f"[ImageCollection] Request failed: {e}")
                return None

            if response.status_code == 200:
                return response

            if response.status_code == 401:
                print(f"[ImageCollection] Token expired ({response.status_code}), refreshing...")
                self.access_token = self.get_access_token()
                if not self.access_token:
                    return response
                continue

            if response.status_code in (403, 429):
                if self._switch_to_next_credentials(response.status_code):
                    continue
                if attempt < retries:
                    _sleep_with_countdown(
                        RETRY_DELAY_SECONDS,
                        f"[ImageCollection] Rate limited ({response.status_code}). Waiting 3 minutes before retry {attempt + 1}/{retries}...",
                    )
                    continue
                print(f"[ImageCollection] Rate limit persisted after {retries} attempts.")
                return response

            return response

        return response

    # ---------- main API ----------

    def _image_record(self, product: ImageProduct, status: str, path=None, message: str = "") -> dict[str, str | None]:
        requested_date = self.time_from[:10] if self.time_from else "N/A"
        return {
            "status": status,
            "path": path,
            "requested_date": requested_date,
            "actual_date": requested_date if status == "available" else "N/A",
            "collection": product.collection_label,
            "message": message,
        }

    @staticmethod
    def _looks_like_empty_image(content: bytes) -> bool:
        try:
            from PIL import Image
        except ImportError:
            return False

        try:
            with Image.open(io.BytesIO(content)) as image:
                mode = image.mode
                extrema = image.getextrema()
        except Exception:
            return False

        if not extrema:
            return False
        if mode in ("RGBA", "LA"):
            alpha_extrema = extrema[-1]
            if alpha_extrema == (0, 0):
                return True
        return all(channel_extrema == (0, 0) for channel_extrema in extrema)

    def fetch_one(self, key):
        """
        Fetch a single product by keyword (e.g. 'true_color', 'wqi', 'chla')
        and save it to disk.

        Returns:
            dict: structured metadata for the requested image product.
        """
        product = self._product_for_key(key)

        payload = self._build_payload(product)
        response = self._post(payload)

        if response is None:
            print(f"[ImageCollection] No response for key '{key}'.")
            return self._image_record(product, "unavailable", message="No API response.")

        if response.status_code != 200:
            response_text = getattr(response, "text", "") or ""
            message = f"API error {response.status_code}"
            if response_text:
                message = f"{message}: {response_text[:300]}"
            print(f"[ImageCollection] {message}")
            return self._image_record(product, "unavailable", message=message)

        if not response.content:
            print(f"[ImageCollection] Empty response for key '{key}'.")
            return self._image_record(product, "unavailable", message="Empty image response.")

        if self._looks_like_empty_image(response.content):
            print(f"[ImageCollection] No image data available for key '{key}' on {self.time_from[:10]}.")
            return self._image_record(product, "unavailable", message="No image data available for requested date.")

        filename = f"{self.tile_name}_{product.key}.{product.file_extension}"
        out_path = os.path.join(self.output_dir, filename)

        with open(out_path, "wb") as f:
            f.write(response.content)

        print(f"[ImageCollection] Saved '{key}' to {out_path}")
        return self._image_record(product, "available", path=out_path, message="Image saved.")

    def run(self, keys):
        """
        Convenience method:
        User passes a list of keywords, we match them to evalscripts,
        request the images, and save them.

        Available evalscripts for images:
        - 'true_color': Sentinel-2 optimized true color
        - 'chla': Chlorophyll-a concentration
        - 'cdom': Colored Dissolved Organic Matter
        - 'turb': Turbidity
        - 'doc': Dissolved Organic Carbon
        - 'cya': Cyanobacteria Index
        - 'surface_temperature': Sentinel-3 land surface temperature

        Example:
            collection.run(['true_color', 'chla', 'surface_temperature'])

        Returns:
            dict: {key: image_metadata, ...}
        """
        results = {}
        for key in keys:
            try:
                results[key] = self.fetch_one(key)
            except KeyError as e:
                print(e)
                placeholder = ImageProduct(
                    key=key,
                    evalscript="",
                    collection_label="unknown",
                    data_sources=(),
                )
                results[key] = self._image_record(placeholder, "unavailable", message=str(e))
        return results
