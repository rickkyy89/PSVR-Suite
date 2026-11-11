"""Blue light candidates, without assigning identities or claiming a pose.

PSVR lamps are diffuse patches, not point markers. Keep their area/bounding
box for inspection; centroid correspondences need a measured rig and matching.
"""

from dataclasses import dataclass

import numpy as np

from .camera_backend import import_cv2


@dataclass(frozen=True)
class BlueLedConfig:
    hue_min: int = 85
    hue_max: int = 135
    saturation_min: int = 80
    value_min: int = 70
    min_area: int = 6
    max_area: int = 6000
    opening_size: int = 0
    include_white_cores: bool = False

    def __post_init__(self):
        if not 0 <= self.hue_min <= self.hue_max <= 179:
            raise ValueError("hue bounds must be ordered in 0..179")
        if not (0 <= self.saturation_min <= 255 and 0 <= self.value_min <= 255):
            raise ValueError("saturation/value thresholds must be in 0..255")
        if not 1 <= self.min_area <= self.max_area:
            raise ValueError("area bounds must be positive and ordered")
        if self.opening_size not in (0, 3, 5):
            raise ValueError("opening_size must be 0, 3 or 5")


class BlueLedDetector:
    def __init__(self, config=None, module=None):
        self.config = config or BlueLedConfig()
        self.cv2 = import_cv2(module)

    def detect(self, image):
        """Return unlabelled candidate dictionaries; never infer LED IDs by sort."""
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
                or image.ndim != 3 or image.shape[2] != 3 or image.size == 0):
            raise ValueError("LED detection requires a nonempty uint8 BGR image")
        cv2, c = self.cv2, self.config
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([c.hue_min, c.saturation_min, c.value_min], np.uint8),
                           np.array([c.hue_max, 255, 255], np.uint8))
        if c.include_white_cores:
            halo = cv2.inRange(hsv,np.array([c.hue_min,40,70],np.uint8),
                               np.array([c.hue_max,255,255],np.uint8))
            near_blue = cv2.dilate(halo,np.ones((7,7),np.uint8))
            white = cv2.inRange(hsv,np.array([0,0,max(200,c.value_min)],np.uint8),
                                np.array([179,90,255],np.uint8))
            count, components, stats, _ = cv2.connectedComponentsWithStats(white,8)
            # Collect the selected white cores into one union mask and dilate
            # once: binary dilation distributes over unions, so a single
            # full-frame dilation replaces one per component.
            cores = None
            for label in range(1,count):
                x,y,width,height,area = (int(v) for v in stats[label])
                if area > c.max_area:
                    continue
                local = components[y:y+height,x:x+width] == label
                if np.count_nonzero(near_blue[y:y+height,x:x+width][local]) >= 2:
                    if cores is None:
                        cores = np.zeros_like(mask)
                    cores[y:y+height,x:x+width][local] = 255
            if cores is not None:
                mask = cv2.bitwise_or(mask,cores)
                attached = cv2.bitwise_and(halo,cv2.dilate(cores,np.ones((5,5),np.uint8)))
                mask = cv2.bitwise_or(mask,attached)
        if c.opening_size:
            kernel = np.ones((c.opening_size, c.opening_size), np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        candidates = []
        for label in range(1, count):
            x, y, width, height, area = (int(v) for v in stats[label])
            if not c.min_area <= area <= c.max_area:
                continue
            # A clipped patch has a biased centre and cannot supply a correspondence.
            if x == 0 or y == 0 or x + width == image.shape[1] or y + height == image.shape[0]:
                continue
            candidates.append({
                "center_px": [float(v) for v in centroids[label]],
                "area_px": area,
                "bbox_px": [x, y, width, height],
            })
        return candidates

    def annotate(self, image, candidates):
        result = image.copy()
        for candidate in candidates:
            x, y, width, height = candidate["bbox_px"]
            self.cv2.rectangle(result, (x, y), (x + width - 1, y + height - 1), (0, 255, 0), 1)
            center = tuple(int(round(v)) for v in candidate["center_px"])
            self.cv2.circle(result, center, 3, (0, 0, 255), 1)
        return result
