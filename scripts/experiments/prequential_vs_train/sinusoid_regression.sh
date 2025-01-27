python train.py --multirun hydra/launcher=mila_thomas save_dir=/network/scratch/t/thomas.jiralerspong/icl/logs \
    seed=0 \
    dataset=regression/sinusoid \
    task=meta_optimizer \
    ++task.meta_objective=prequential,train \
    ++logger.tags=[experiments/prequential_vs_train/regression]
