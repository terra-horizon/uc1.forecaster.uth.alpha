import boto3
from botocore.client import Config
import os

# --- CONFIGURATION ---
MINIO_ENDPOINT = "https://195.251.57.17"
# MINIO_ENDPOINT = "http://terra-minio:9000" # running in docker container
ACCESS_KEY = "terra-user"
SECRET_KEY = "4y14Ty3WHTR0hJsKkwXM"
BUCKET_NAME = "terra-bucket"

def get_s3_client():
    return boto3.resource(
        's3',
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        config=Config(signature_version='s3v4'),
        region_name='us-east-1',
        verify=False
    )

def main():
    s3 = get_s3_client()
    bucket = s3.Bucket(BUCKET_NAME)
    # Create test file
    filename = "test_from_python2.txt"
    with open(filename, "w") as f:
        f.write("Hello Terra! This file was uploaded via Python Boto3.")

    print(f"--- Uploading {filename} ---")
    try:
        # Upload to Bucket
        bucket.upload_file(filename, filename)
        print("Successfull upload!")

        # List Bucket files
        print("\n--- Bucket files ---")
        for obj in bucket.objects.all():
            print(f"Found: {obj.key} (Size: {obj.size} bytes)")

        # Download file with new name
        download_name = "downloaded_from_minio.txt"
        print(f"\n--- Download {download_name} ---")
        bucket.download_file(filename, download_name)
        
        if os.path.exists(download_name):
            print("Download successful!")
            with open(download_name, 'r') as f:
                print(f"Read file: {f.read()}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Remove tmp files
        if os.path.exists(filename): os.remove(filename)
        # if os.path.exists(download_name): os.remove(download_name)

if __name__ == "__main__":
    main()