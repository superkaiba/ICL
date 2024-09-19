python train.py --multirun hydra/launcher=mila_eric save_dir=/home/mila/e/eric.elmoznino/scratch/prequential_icl/logs \
    seed=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14 \
    dataset=symbolic/mastermind \
    task=sgd_optimizer \
    ++task.loss_fn._target_=utils.CrossEntropyLossFlat \
    ++predictor.in_features=48 ++predictor.out_features=18 \
    ++predictor.n_layers=5 ++predictor.h_dim=256 \
    ++dataset.train_dataset.one_hot_y=False \
    ++datamodule.batch_size=64 \
    ++dataset.train_dataset.n_samples=3000 \
    ++task.inner_epochs=2000 \
    ++task.lr=0.0001 \
    ++trainer.max_epochs=2000000 \
    ++callbacks.monitor=val_loss \
    ++callbacks.min_delta=1e-3 \
    ++callbacks.patience=10 \
    ++trainer.gradient_clip_val=0.05 \
    ++logger.tags=[experiments/sgd_vs_prequential/symbolic]
