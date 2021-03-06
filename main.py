import time
import os
import torch
import numpy as np
from torch.optim import lr_scheduler, Adam
from torchvision import utils
from tensorboardX import SummaryWriter
from pcnnpp.model import init_model, PixelCNN
from pcnnpp import config
from pcnnpp.data import DatasetSelection, rescaling_inv
from pcnnpp.utils.functions import get_loss_function
from pcnnpp.utils.evaluation import sample, plot_loss, evaluate, plot_evaluation, show_extreme_cases

if config.use_tpu:
    import torch_xla
    import torch_xla.core.xla_model as xm
    import torch_xla.debug.metrics as met
    import torch_xla.distributed.parallel_loader as pl
    import torch_xla.distributed.xla_multiprocessing as xmp
    import torch_xla.utils.utils as xu

if config.use_arg_parser:
    import pcnnpp.utils.argparser

    config = pcnnpp.utils.argparser.parse_args()

# reproducibility
torch.set_default_tensor_type('torch.FloatTensor')
torch.manual_seed(config.seed)
np.random.seed(config.seed)

model = None
dataset_train = None
dataset_validation = None


def train():
    global model
    validation_losses = []
    train_losses = []
    print('starting training')
    # starting up data loaders
    print("loading training data")
    dataset_train = DatasetSelection(train=True, classes=config.normal_classes)
    print('loading validation data')
    dataset_validation = DatasetSelection(train=False, classes=config.normal_classes)
    print('loading test data')
    dataset_test = DatasetSelection(train=False, classes=config.test_classes)

    train_sampler = None
    validation_sampler = None
    test_sampler = None
    if config.use_tpu:
        print('creating tpu sampler')
        train_sampler = torch.utils.data.distributed.DistributedSampler(
            dataset_train,
            num_replicas=xm.xrt_world_size(),
            rank=xm.get_ordinal(),
            shuffle=True
        )
        validation_sampler = torch.utils.data.distributed.DistributedSampler(
            dataset_validation,
            num_replicas=xm.xrt_world_size(),
            rank=xm.get_ordinal(),
            shuffle=True
        )
        test_sampler = torch.utils.data.distributed.DistributedSampler(
            dataset_test,
            num_replicas=xm.xrt_world_size(),
            rank=xm.get_ordinal(),
            shuffle=False
        )
        print('tpu samplers created')
    train_loader = dataset_train.get_dataloader(sampler=train_sampler, shuffle=not config.use_tpu)
    validation_loader = dataset_validation.get_dataloader(sampler=validation_sampler, shuffle=not config.use_tpu, )
    test_loader = dataset_test.get_dataloader(sampler=test_sampler, shuffle=False, )

    input_shape = dataset_validation.input_shape()
    loss_function = get_loss_function(input_shape)

    # setting up tensorboard data summerizer
    writer = SummaryWriter(log_dir=os.path.join(config.log_dir, config.model_name))

    # initializing model
    model = init_model(input_shape)

    print("initializing optimizer & scheduler")
    optimizer = Adam(model.parameters(), lr=config.lr)
    scheduler = lr_scheduler.MultiplicativeLR(optimizer, lr_lambda=config.lr_multiplicative_factor_lambda,
                                      last_epoch=config.start_epoch - 1)

    def train_loop(data_loader, writes=0):
        if torch.cuda.is_available():
            torch.cuda.synchronize(device=config.device)
        train_loss = 0.
        last_train_loss = 0.
        new_writes = 0
        time_ = time.time()
        if config.use_tpu:
            tracker = xm.RateTracker()
        model.train()
        for batch_idx, (input, _) in enumerate(data_loader):
            input = input.to(config.device, non_blocking=True)
            if config.noising_factor is not None:
                false_input = input + config.noising_factor * config.noise_function(input.shape)
                false_input.clamp_(min=-1, max=1)
                output = model(false_input)
            else:
                output = model(input)
            loss = loss_function(input, output)
            optimizer.zero_grad()
            loss.backward()
            if config.use_tpu:
                xm.optimizer_step(optimizer)
                tracker.add(config.batch_size)
            else:
                optimizer.step()
            train_loss += loss
            if config.print_every and (batch_idx + 1) % config.print_every == 0 :
                deno = config.print_every * config.batch_size * np.prod(input_shape) * np.log(2.)
                if not config.use_tpu:
                    writer.add_scalar('train/bpd', (train_loss / deno), writes + new_writes)

                print('\t{:3d}/{:3d} - loss : {:.4f}, time : {:.3f}s'.format(
                    batch_idx // config.print_every + 1,
                    len(train_loader) // config.print_every,
                    (train_loss / deno),
                    (time.time() - time_)
                ))
                last_train_loss = train_loss
                train_loss = 0.
                new_writes += 1
                time_ = time.time()
            del input, _, loss, output

        return new_writes, (last_train_loss / deno)

    def validation_loop(data_loader, writes=0):
        if torch.cuda.is_available():
            torch.cuda.synchronize(device=config.device)
        model.eval()
        test_loss = 0.
        with torch.no_grad():
            for batch_idx, (input, _) in enumerate(data_loader):
                input = input.to(config.device, non_blocking=True)
                output = model(input)
                loss = loss_function(input, output)
                test_loss += loss
                del loss, output

            deno = batch_idx * config.batch_size * np.prod(input_shape) * np.log(2.)
            writer.add_scalar('validation/bpd', (test_loss / deno), writes)
            print('\t{}epoch {:4} validation loss : {:.4f}'.format(
                '' if not config.use_tpu else xm.get_ordinal(),
                epoch,
                (test_loss / deno)
            ),
                flush=True
            )

            if config.save_interval and (epoch + 1) % config.save_interval == 0:
                torch.save(model.state_dict(), config.models_dir + '/{}_{}.pth'.format(config.model_name, epoch))
                print('\tsampling epoch {:4}'.format(
                    epoch
                ))
                sample_t = sample(model, input_shape)
                sample_t = rescaling_inv(sample_t)
                utils.save_image(sample_t, config.samples_dir + '/{}_{}.png'.format(config.model_name, epoch),
                                 nrow=5, padding=0)
            return test_loss / deno

    try:
        writes = 0
        for epoch in range(config.start_epoch, config.max_epochs):
            print('epoch {:4} - lr: {}'.format(epoch, optimizer.param_groups[0]["lr"]))
            if config.use_tpu:
                para_loader = pl.ParallelLoader(train_loader, [config.device])
                train_loop(para_loader.per_device_loader(config.device), writes)
                xm.master_print("\tFinished training epoch {}".format(epoch))
            else:
                new_writes, train_loss = train_loop(train_loader, writes)
                train_losses.append(train_loss)
                writes += new_writes

            # learning rate schedule
            scheduler.step(epoch)

            if config.use_tpu:
                para_loader = pl.ParallelLoader(validation_loader, [config.device])
                validation_loop(para_loader.per_device_loader(config.device), writes)
            else:
                validation_loss = validation_loop(validation_loader, writes)
                validation_losses.append(validation_loss)
                model_name = f'{"DCNNpp" if config.noising_factor is not None else "PCNNpp"}-E{epoch}'
                # evaluation and loss tracking
                if config.plot_every and (epoch + 1) % config.plot_every == 0:
                    plot_loss(
                        train_losses,
                        validation_losses,
                        model_name=f'{"DCNNpp" if config.noising_factor is not None else "PCNNpp"}-{optimizer.param_groups[0]["lr"]:.7f}'
                        , save_path=config.losses_dir + f'/Losses{model_name}.png',
                    )

                if config.evaluate_every and (epoch + 1) % config.evaluate_every == 0:
                    eval_data = evaluate(model, dataset_test, test_loader)
                    plot_evaluation(
                        eval_data,
                        model_name=f'{"DCNNpp" if config.noising_factor is not None else "PCNNpp"}-E{epoch}',
                        save_path=config.evaluation_dir + f'/EvalPlot{model_name}.png'
                    )
                    show_extreme_cases(
                        eval_data,
                        model_name=model_name,
                        save_dir=config.extreme_cases_dir
                    )

            writes += 1
    except KeyboardInterrupt:
        pass
    return model, train_losses, validation_losses


if config.use_tpu:
    def train_on_tpu():
        def trainer(rank, CONFIG):
            global config
            config = CONFIG
            config.device = xm.xla_device()
            torch.set_default_tensor_type('torch.FloatTensor')
            train()

        xmp.spawn(trainer, args=(config,), nprocs=config.num_cores,
                  start_method='fork')

if config.train:
    if config.use_tpu:
        train_on_tpu()
    else:
        train()
