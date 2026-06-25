source /home/chenbei/.venv/bin/activate
export CUDA_HOME=/mnt/shared-storage-gpfs2/gpfs2-shared-public/soft/cuda/12.8
export PATH=$CUDA_HOME/bin:$PATH
vllm serve /mnt/shared-storage-gpfs2/gpfs2-shared-public/huggingface/zskj-hub/models--RUC-DataLab--DeepAnalyze-8B --served-model-name DeepAnalyze-8B