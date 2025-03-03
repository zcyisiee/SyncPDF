import logging
from pathlib import Path

import cv2
import httpx
import msgpack
import numpy as np
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from babeldoc.docvision.doclayout import DocLayoutModel
from babeldoc.docvision.doclayout import YoloBox
from babeldoc.docvision.doclayout import YoloResult

logger = logging.getLogger(__name__)


def encode_image(image) -> bytes:
    """Read and encode image to bytes

    Args:
        image: Can be either a file path (str) or numpy array
    """
    if isinstance(image, str):
        if not Path(image).exists():
            raise FileNotFoundError(f"Image file not found: {image}")
        img = cv2.imread(image)
        if img is None:
            raise ValueError(f"Failed to read image: {image}")
    else:
        img = image

    # logger.debug(f"Image shape: {img.shape}")
    encoded = cv2.imencode(".jpg", img)[1].tobytes()
    # logger.debug(f"Encoded image size: {len(encoded)} bytes")
    return encoded


@retry(
    stop=stop_after_attempt(3),  # 最多重试3次
    wait=wait_exponential(
        multiplier=1, min=1, max=10
    ),  # 指数退避策略，初始1秒，最大10秒
    retry=retry_if_exception_type((httpx.HTTPError, Exception)),  # 针对哪些异常重试
    before_sleep=lambda retry_state: logger.warning(
        f"Request failed, retrying in {retry_state.next_action.sleep} seconds... "
        f"(Attempt {retry_state.attempt_number}/3)"
    ),
)
def predict_layout(
    image,
    host: str = "http://localhost:8000",
    imgsz: int = 1024,
):
    """
    Predict document layout using the MOSEC service

    Args:
        image: Can be either a file path (str) or numpy array
        host: Service host URL
        imgsz: Image size for model input

    Returns:
        List of predictions containing bounding boxes and classes
    """
    # Prepare request data
    if not isinstance(image, list):
        image = [image]
    image_data = [encode_image(image) for image in image]
    data = {
        "image": image_data,
        "imgsz": imgsz,
    }

    # Pack data using msgpack
    packed_data = msgpack.packb(data, use_bin_type=True)
    logger.debug(f"Packed data size: {len(packed_data)} bytes")

    # Send request
    logger.debug(f"Sending request to {host}/inference")
    response = httpx.post(
        f"{host}/inference",
        data=packed_data,
        headers={
            "Content-Type": "application/msgpack",
            "Accept": "application/msgpack",
        },
        timeout=300,
        follow_redirects=True,
    )

    logger.debug(f"Response status: {response.status_code}")
    logger.debug(f"Response headers: {response.headers}")

    if response.status_code == 200:
        try:
            result = msgpack.unpackb(response.content, raw=False)
            return result
        except Exception as e:
            logger.exception(f"Failed to unpack response: {e!s}")
            raise
    else:
        logger.error(f"Request failed with status {response.status_code}")
        logger.error(f"Response content: {response.content}")
        raise Exception(
            f"Request failed with status {response.status_code}: {response.text}",
        )


class RpcDocLayoutModel(DocLayoutModel):
    """DocLayoutModel implementation that uses RPC service."""

    def __init__(self, host: str = "http://localhost:8000"):
        """Initialize RPC model with host address."""
        self.host = host
        self._stride = 32  # Default stride value
        self._names = ["text", "title", "list", "table", "figure"]

    @property
    def stride(self) -> int:
        """Stride of the model input."""
        return self._stride

    def predict(self, image, imgsz=1024, **kwargs) -> list[YoloResult]:
        """Predict the layout of document pages using RPC service."""
        # Handle single image input
        if isinstance(image, np.ndarray) and len(image.shape) == 3:
            image = [image]

        results = []
        for img in image:
            preds = predict_layout([img], host=self.host, imgsz=imgsz)
            if len(preds) > 0:
                for pred in preds:
                    boxes = [
                        YoloBox(
                            None, np.array(x["xyxy"]), np.array(x["conf"]), x["cls"]
                        )
                        for x in pred["boxes"]
                    ]
                    results.append(
                        YoloResult(
                            boxes=boxes,
                            names={int(k): v for k, v in pred["names"].items()},
                        ),
                    )
            else:
                results.append(YoloResult(boxes_data=np.array([]), names=[]))

        return results

    @staticmethod
    def from_host(host: str) -> "RpcDocLayoutModel":
        """Create RpcDocLayoutModel from host address."""
        return RpcDocLayoutModel(host=host)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    # Test the service
    try:
        # Use a default test image if example/1.png doesn't exist
        image_path = "example/1.png"
        if not Path(image_path).exists():
            print(f"Warning: {image_path} not found.")
            print("Please provide the path to a test image:")
            image_path = input("> ")

        logger.info(f"Processing image: {image_path}")
        result = predict_layout(image_path)
        print("Prediction results:")
        print(result)
    except Exception as e:
        print(f"Error: {e!s}")
