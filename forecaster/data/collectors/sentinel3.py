import requests
import json
import time
import os
import csv
from datetime import date, datetime
from config import CDSE_Credentials as CDSE_Credentials

DATA_DIR = "forecaster/data"
RETRY_DELAY_SECONDS = 180  # 3 minutes

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

class Sentinel3Collection:
    def __init__(self, time_interval=None, bbox=[], dir=DATA_DIR):
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

        # Evalscript
        self.evalscript = """
//VERSION=3
function setup() {
    return {
        input: [{ bands: ["S8", "dataMask"] }],
        output: [
            { id: "data", bands: 1 },
            { id: "dataMask", bands: 1 }
        ]
    };
}

function evaluatePixel(samples) {
    // Cloud Masking:
    // S8 (10.8µm) is the standard thermal band for day/night.
    // Water is typically ~280-300K.
    // Clouds are much colder (e.g. < 270K).
    // Filter out anything below 270K (-3.15 C) to remove cloud tops.
    
    if (samples.S8 < 270) {
        return {
            data: [samples.S8],
            dataMask: [0] // Mask as invalid
        };
    }

    return {
        data: [samples.S8],
        dataMask: [samples.dataMask]
    };
}
"""
        
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
        """Send request to Copernicus API and retrieve statistics with retry."""
        if not self.access_token:
            print("No access token available. Cannot proceed.")
            return None

        # Build payload for Sentinel-3
        payload = {
            "input": {
                "bounds": {
                    "bbox": bbox, 
                },
                "data": [{
                    "type": "sentinel-3-slstr",
                    "dataFilter": {
                        "timeRange": {
                            "from": f"{time_interval[0]}T00:00:00Z",  
                            "to": f"{time_interval[1]}T23:59:59Z"
                        },
                        "maxCloudCoverage": 100 
                    }
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
            "output": {
                "format": "JSON",
                "statistics": ["mean"], 
                "includeInvalidPixels": False
            }
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
        output_dir = os.path.join(self.dir, 'statistical_s3')
        if not os.path.exists(output_dir): 
            os.makedirs(output_dir) 
        
        for slot in self.slots:
            image_count += 1
            response = self.get_request(self.evalscript, slot, self.bbox)
            
            if not response:
                print("[Statistics] No response received; skipping this slot.")
                continue

            # Optional: save raw response for debugging?
            # with open(os.path.join(output_dir, 'last_response.json'), 'w') as json_file:
            #     json.dump(response.json(), json_file, indent=4)

            if response and response.status_code == 200:
                values = self.compute_values(response.json())
                filename = f'{output_dir}/{" - ".join(slot)}_{image_count}.json'
                with open(filename, 'w') as json_file:
                    json.dump(values, json_file, indent=4)
                print(f"Data saved to {filename}")

    def compute_values(self, response_json):
        results = []

        types = ['mean']
        for interval in response_json["data"]:
            date_str = interval["interval"]["from"][:10]  # Extract date in yyyy-mm-dd format
            # Using same format as S2: dd-mm-yyyy for consistency
            date_fmt = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d-%m-%Y") 
            
            outputs = interval.get("outputs", {})
            data = outputs.get("data", {})
            bands = data.get("bands", {})
            
            results_mean = []

            for i in types:
                # Use S8 stats (Band 0 of output)
                S07 = bands.get("B0", {}).get("stats", {}).get(i, None)

                # Handle NaN values
                if any(val in [None, "NaN", float("NaN")] or val is None for val in [S07]):
                    continue

                # Convert to float
                S07 = float(S07)
                
                if S07 == 0.0:
                    continue
                    
                temperature = S07 - 273.15
                entry = {
                    "s3_surface_temperature": temperature,
                }
                
                # Store results
                match i:
                    case 'mean':
                        results_mean.append(entry)

            if results_mean:
                results.append({
                    date_fmt: {
                        "mean": results_mean[0],
                    }
                })

        return results

    def make_csv_per_stat(self, json_folder, output_folder):
        os.makedirs(output_folder, exist_ok=True)
        data = []
        
        if os.path.exists(json_folder):
            for filename in os.listdir(json_folder):
                if filename.endswith('.json'):
                    filepath = os.path.join(json_folder, filename)
                    with open(filepath, 'r') as f:
                        try:
                            content = json.load(f)
                            if isinstance(content, list):
                                data.extend(content)
                        except json.JSONDecodeError:
                            print(f"Error decoding {filename}")

        # Collect all available elements
        elements = set()
        for record in data:
            for date_val, values in record.items():
                if values.get("mean"):
                    elements.update(values["mean"].keys())

        elements = sorted(elements)

        # Initialize data containers
        aggregated_data = {}

        # Fill the data
        for record in data:
            for date_val, values in record.items():
                if date_val not in aggregated_data:
                     aggregated_data[date_val] = {el: [] for el in elements}
                
                for el in elements:
                    val = 0.0
                    if values.get("mean") and el in values["mean"]:
                        val = values["mean"][el]
                    aggregated_data[date_val][el].append(val)

        filename = os.path.join(output_folder, f"s3_mean_metrics.csv")
        with open(filename, mode="w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["date"] + elements)
            writer.writeheader()
            
            sorted_dates = sorted(aggregated_data.keys(), key=lambda d: datetime.strptime(d, "%d-%m-%Y"))
            
            for d in sorted_dates:
                row = {"date": d}
                for el in elements:
                    vals = aggregated_data[d][el]
                    row[el] = sum(vals) / len(vals) if vals else 0.0
                writer.writerow(row)

        print(f"All Sentinel-3 CSV files (mean) saved inside '{output_folder}' folder!")

    def run(self, json_output_folder=None, csv_output_folder=None):
        """
        Run the collection pipeline.
        """
        if json_output_folder is None:
            json_output_folder = os.path.join(self.dir, "statistical_s3")
        if csv_output_folder is None:
            csv_output_folder = os.path.join(self.dir, "csv")
            
        # Ensure json output folder matches where save_data writes
        # save_data writes to self.dir/statistical_s3 currently. 
        # Let's align them.
        self.save_data() # This writes to self.dir/statistical_s3
        
        # Now make CSVs
        self.make_csv_per_stat(json_folder=os.path.join(self.dir, "statistical_s3"), output_folder=csv_output_folder)

