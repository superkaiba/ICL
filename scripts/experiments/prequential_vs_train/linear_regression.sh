export HYDRA_FULL_ERROR=1
python train.py --multirun hydra/launcher=mila_thomas save_dir=/network/scratch/t/thomas.jiralerspong/icl/logs \
    seed=0 \
    dataset=regression/linear \
    task=meta_optimizer \
    ++task.meta_objective=prequential \
    ++dataset.train_dataset.x_dim=3 \
    ++dataset.train_dataset.noise=0.2 \
    ++logger.tags=[experiments/prequential_vs_train/regression]


