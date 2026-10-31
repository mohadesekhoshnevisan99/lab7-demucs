#!/bin/sh
set -eu

echo "Checking Kubernetes connection..."
kubectl cluster-info >/dev/null

echo "Checking MinIO credentials..."
kubectl get secret minio-auth >/dev/null

echo "Deploying Redis..."
kubectl apply -f redis/redis-deployment.yaml
kubectl apply -f redis/redis-service.yaml
kubectl rollout status deployment/redis --timeout=120s

echo "Deploying MinIO..."
kubectl apply -f minio/minio-deployment.yaml
kubectl apply -f minio/minio-service.yaml
kubectl rollout status deployment/minio --timeout=180s

echo "Deploying REST..."
kubectl apply -f rest/rest-deployment.yaml
kubectl apply -f rest/rest-service.yaml
kubectl rollout status deployment/rest --timeout=120s

echo "Initializing MinIO buckets..."
kubectl exec deployment/rest -- python -c '
import os
from minio import Minio

m = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=False
)

for bucket in ("queue", "output"):
    if not m.bucket_exists(bucket):
        m.make_bucket(bucket)
    print("Ready:", bucket)
'

echo "Deploying Worker..."
kubectl apply -f worker/worker-deployment.yaml
kubectl rollout status deployment/worker --timeout=180s

echo "Applying REST Ingress..."
kubectl apply -f rest/rest-ingress.yaml

echo "Final Kubernetes status:"
kubectl get pods -o wide
kubectl get services
kubectl get ingress

echo "LAB 7 DEPLOYMENT COMPLETED"
