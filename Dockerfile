FROM python:3.7.10

WORKDIR /app

COPY ./ ./
RUN pip install  google-cloud-spanner==1.19.1 google_cloud_storage==1.43.0

RUN pip install -e .

RUN pip install pytest absl-py google-api-core portpicker

ENV GOOGLE_APPLICATION_CREDENTIALS '/app/spanner-key.json'
