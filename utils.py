from os import PathLike
import json
from importlib import import_module
from pathlib import Path
import torch
from torchvision.transforms.functional import (
    resize,
    center_crop,
    InterpolationMode,
)
from torchvision.utils import make_grid
from ignite.engine import _prepare_batch
from ignite.utils import convert_tensor
from typing import (
    Any,
    Iterable,
    Optional,
    Union,
    Dict,
    Tuple,
    Callable,
    Sequence,
    List,
)


def get_class(path: str) -> Any:
    """
    Get class from import path.

    Example
    -------
        cl = get_class("torch.optim.Adam)
        optimizer = cl(parameters, lr=1e-5)
    """
    parts = path.split(".")
    module_path = ".".join(parts[:-1])
    class_name = parts[-1]
    module = import_module(module_path)
    return getattr(module, class_name)


def create_object_from_config(config: Dict[str, Any], **kwargs) -> Any:
    """
    Create object from dictionary configuration.
    Dictionary should have a ``class`` entry and an optional ``params`` entry.

    Example
    -------
        config = {"class": "torch.optim.Adam", "params": {"lr": 1e-5}}
        optimizer = create_object_from_config(config)
    """
    obj_class = get_class(config["class"])
    params = dict(config.get("params", {}))
    params.update(kwargs)
    return obj_class(**params)


def load_model_checkpoint(
    path: Union[str, PathLike],
    config_path: Optional[Union[str, PathLike]] = None,
) -> Tuple[torch.nn.Module, Dict[str, Any]]:
    """Load model from checkpoint."""
    path = Path(path)

    if config_path is None:
        config_path = path.parent / "config.json"
    with open(config_path, "r") as fp:
        config = json.load(fp)

    model = create_object_from_config(config["model"])
    ckpt = torch.load(path, map_location="cpu")
    model.load_state_dict(ckpt["model"])
    return model, config


def predict_test_images(
    model: torch.nn.Module,
    data: Optional[Iterable],
    device: torch.device,
    prepare_batch: Callable = _prepare_batch,
    input_transform: Callable[[Any], Any] = lambda x: x,
    output_transform: Callable[[Any, Any, Any], Any] = (
        lambda x, y, y_pred: [x, y_pred, y]
    ),
    resize_to_fit: bool = True,
    interpolation: InterpolationMode = InterpolationMode.NEAREST,
    antialias: bool = True,
    num_cols: int = None,
    amp_mode: Optional[str] = None,
    non_blocking: bool = False,
) -> List[torch.Tensor]:
    model.eval()

    images = []
    for batch in iter(data):
        x, y = prepare_batch(batch, device, non_blocking)
        x = input_transform(x)
        with torch.inference_mode():
            with torch.autocast(
                device_type=device.type, enabled=amp_mode == "amp"
            ):
                y_pred = model(x)

        y_pred = y_pred.type(y.dtype)
        x, y, y_pred = convert_tensor(
            x=(x, y, y_pred),
            device=torch.device("cpu"),
            non_blocking=non_blocking,
        )

        output = output_transform(x, y, y_pred)
        batch_sizes = [len(e) for e in output]
        assert all(s == batch_sizes[0] for s in batch_sizes)

        for i in range(batch_sizes[0]):
            grid = _combine_test_images(
                images=[e[i] for e in output],
                resize_to_fit=resize_to_fit,
                interpolation=interpolation,
                antialias=antialias,
                num_cols=num_cols,
            )
            images.append(grid)
    return images


def _combine_test_images(
    images: Sequence[torch.Tensor],
    resize_to_fit: bool = True,
    interpolation: InterpolationMode = InterpolationMode.NEAREST,
    antialias: bool = True,
    num_cols: int = None,
) -> torch.Tensor:
    for image in images:
        assert len(image.shape) == 3
        assert image.size(0) in (1, 3)

    max_h = max(image.size(1) for image in images)
    max_w = max(image.size(2) for image in images)

    transformed = []
    for image in images:
        C, H, W = image.shape
        if H != max_h or W != max_w:
            if resize_to_fit:
                image = resize(
                    image,
                    size=(max_h, max_w),
                    interpolation=interpolation,
                    antialias=antialias,
                )
            else:
                image = center_crop(image, output_size=(max_h, max_w))
        if C == 1:
            image = image.repeat((3, 1, 1))
        image = image.clamp(0.0, 1.0)
        transformed.append(image)

    if len(transformed) == 1:
        return transformed[0]
    else:
        return make_grid(
            tensor=transformed,
            normalize=False,
            nrow=num_cols or len(transformed),
        )