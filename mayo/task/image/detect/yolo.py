import tensorflow as tf
from tensorflow.contrib import slim

from mayo.util import memoize_property
from mayo.task.image.base import ImageTaskBase
from mayo.task.image.detect.util import cartesian, iou


class YOLOv2(ImageTaskBase):
    """
    YOLOv2 image detection algorithm.

    references:
        https://arxiv.org/pdf/1612.08242.pdf
        https://github.com/allanzelener/YAD2K/blob/master/yad2k/models/keras_yolo.py
    """
    def __init__(
            self, session, preprocess, num_classes, shape,
            anchors, object_scale=1, noobject_scale=0.5,
            coordinate_scale=5, class_scale=1,
            num_cells=13, iou_threshold=0.6, score_threshold=0.6,
            nms_iou_threshold=0.5, max_num_boxes=10):
        """
        anchors: a list of anchor boxes [(h, w), ...].
        num_cells: the number of cells to divide the image into a grid.
        iou_threshold:
            the threshold of IOU upper-bound to suppress the absense
            of object in training loss.
        score_threshold:
            the threshold used to filter less confident detections
            during validation.
        nms_iou_threshold:
            the IOU threshold used by non-max suppression during validation.
        """
        super().__init__(session)
        self._anchors = anchors
        self.num_anchors = len(anchors)
        height, width = shape['height'], shape['width']
        if height != width:
            raise ValueError('We expect the image to be square for YOLOv2.')
        self.image_size = height
        self.batch_size = session.batch_size
        if not self.image_size % num_cells:
            raise ValueError(
                'The size of image must be divisible by the number of cells.')
        self.num_cells = num_cells

        self.cell_size = self.image_size / num_cells
        self._base_shape = [num_cells, num_cells, self.num_anchors]

        self.object_scale = object_scale
        self.noobject_scale = noobject_scale
        self.coordinate_scale = coordinate_scale
        self.class_scale = class_scale
        self.iou_threshold = iou_threshold
        self.score_threshold = score_threshold
        self.nms_iou_threshold = nms_iou_threshold
        self.max_num_boxes = max_num_boxes

    @memoize_property
    def anchors(self):
        return tf.constant(self._anchors)

    def _cell_to_global(self, box):
        """
        Transform (batch x num_cells x num_cells x num_anchors x 4)
        raw bounding box (before sigmoid and exponentiation) to the
        full-image bounding box.
        """
        # grid setup
        line = tf.range(0, self.num_cells)
        rows = tf.reshape(line, [self.num_cells, 1])
        rows = tf.tile(rows, [1, self.num_cells])
        cols = tf.reshape(line, [1, self.num_cells])
        cols = tf.tile(cols, [self.num_cells, 1])
        grid = tf.stack([rows, cols], axis=-1)
        grid = tf.reshape(grid, [1, self.num_cells, self.num_cells, 1, 2])
        grid *= self.cell_size
        # box transformation
        yx, hw = tf.split(box, [2, 2], axis=-1)
        yx = grid + tf.sigmoid(yx)
        hw = tf.exp(hw)
        hw *= tf.reshape(self.anchors, [1, 1, 1, self.num_anchors, 2])
        box = tf.concat([yx, hw], axis=-1)
        # normalize box position to 0-1
        return box / self.image_size

    def _truth_to_cell(self, box, label):
        """
        Allocates ground truth bounding boxes and labels into a cell grid of
        objectness, bounding boxes and labels.

        truths:
            a (batch x num_truths x 4) tensor where each row is a
            boudning box denoted by [y, x, h, w].
        labels:
            a (batch x num_truths) tensor of labels.
        returns:
            - a (batch x num_cells x num_cells x num_anchors)
              tensor of objectness.
            - a (batch x num_cells x num_cells x num_anchors x 4)
              tensor of bounding boxes.
            - a (batch x num_cells x num_cells x num_anchors) tensor of labels.
        """
        # normalize the scale of bounding boxes to the size of each cell
        # y, x, h, w are (batch x num_truths)
        y, x, h, w = tf.unstack(box * self.cell_size, axis=-1)

        # indices of the box-containing cells (batch x num_truths)
        # any position within [k * cell_size, (k + 1) * cell_size)
        # gets mapped to the index k.
        row, col = (tf.cast(tf.floor(v), tf.int32) for v in (y, x))

        # ious: batch x num_truths x num_boxes
        pair_box, pair_anchor = cartesian(
            tf.stack([h, w], axis=-1), self.anchors)
        ious = iou(pair_box, pair_anchor)
        # box indices: (batch x num_truths)
        _, anchor_index = tf.nn.top_k(ious)
        # coalesced indices (batch x num_truths x 3), where 3 values denote
        # (#row, #column, #anchor)
        index = tf.stack([row, col, anchor_index], axis=-1)

        # objectness tensor, batch-wise allocation
        # for each (batch), indices (num_truths x 3) are used to scatter values
        # into a (num_cells x num_cells x num_anchors) tensor.
        # resulting in a (batch x num_cells x num_cells x num_anchors) tensor.
        objectness = tf.map_fn(tf.scatter_nd, (index, 1, self._base_shape))

        # boxes
        # best anchors
        best_anchor = tf.gather_nd(self.anchors, anchor_index)
        # (batch x num_truths)
        h_anchor, w_anchor = tf.unstack(best_anchor, axis=-1)
        # adjusted relative to cell row and col (batch x num_truths x 4)
        # FIXME shouldn't we adjust to the center of each grid cell,
        # rather than its top-left?
        box = [y - row, x - col, tf.log(h / h_anchor), tf.log(w / w_anchor)]
        box = tf.stack(box, axis=-1)
        # boxes, batch-wise allocation
        box = tf.scatter_nd(index, box, self._base_shape + [4])

        # labels, batch-wise allocation
        label = tf.scatter_nd(index, label, self._base_shape)
        return objectness, box, label

    def preprocess(self):
        """
        Additional tensor decomposition in preprocessing.

        prediction:
            a (batch x num_cells x num_cells x num_anchors x (5 + num_classes))
            prediction, where each element of the last dimension (5 +
            num_classes) consists of a objectness probability (1), a bounding
            box (4), and a one-hot list (num_classes) of class probabilities.
        truth:
            a (batch x num_objects x 5) tensor, where each item of the
            last dimension (5) consists of a bounding box (4), and a label (1).
        """
        for prediction, truth in super().preprocess():
            data = {}
            obj_p, box_p, hot_p = tf.split(
                prediction, [1, 4, self.num_classes], axis=-1)
            prediction = {
                'object': obj_p,
                'box': box_p,
                'outbox': self._cell_to_global(box_p),
                'class': hot_p,
            }
            # the original box and label values from the dataset
            outbox, label = tf.split(truth, [4, 1], axis=-1)
            obj, box, label = self._truth_to_cell(outbox, label)
            truth = {
                'object': obj,
                'box': box,
                'rawbox': outbox,
                'class': slim.one_hot_encoding(label, self.num_classes),
            }
            yield prediction, truth

    def _eval(self, prediction):
        # filter objects with a low confidence score
        # batch x cell x cell x anchors x classes
        confidence = prediction['object'] * prediction['class']
        # batch x cell x cell x anchors
        classes = tf.argmax(confidence, axis=-1)
        scores = tf.reduce_max(confidence)
        mask = scores >= self.score_threshold
        # only confident objects are left
        boxes = tf.boolean_mask(prediction['outbox'], mask)
        scores = tf.boolean_mask(scores, mask)
        classes = tf.boolean_mask(classes, mask)

        # non-max suppression
        indices = tf.image.non_max_suppression(
            boxes, scores, self.max_num_boxes, self.nms_iou_threshold)
        boxes = tf.gather_nd(boxes, indices)
        scores = tf.gather_nd(scores, indices)
        classes = tf.gather_nd(classes, indices)
        return boxes, scores, classes

    def eval(self, net, prediction, truth):
        boxes, scores, classes = self._eval(prediction)
        ...

    def train(self, net, prediction, truth):
        """Training loss.  """
        num_objects = truth['rawbox'].shape[1]

        # coordinate loss
        xy_p, wh_p = tf.split(prediction['box'], [2, 2], axis=-1)
        xy, wh = tf.split(truth['box'], [2, 2], axis=-1)
        coord_loss = (xy - xy_p) ** 2 + (tf.sqrt(wh) - tf.sqrt(wh_p)) ** 2
        coord_loss = tf.reduce_sum(truth['object'] * coord_loss)
        coord_loss *= self.coordinate_scale

        # objectness loss
        obj_loss = truth['object'] * (1 - prediction['object']) ** 2
        obj_loss = self.object_scale * tf.reduce_sum(obj_loss)

        # class loss
        class_loss = (truth['class'] - prediction['class']) ** 2
        class_loss = tf.reduce_sum(truth['object'] * class_loss)
        class_loss *= self.class_scale

        # no-object loss
        # match shape
        # (batch x num_cells x num_cells x num_anchors x num_objects x 4)
        shape = [
            self.batch_size, self.num_cells, self.num_cells,
            self.num_anchors, 1, 4]
        outbox_p = tf.reshape(prediction['outbox'], shape)
        shape = [self.batch_size, 1, 1, 1, num_objects, 4]
        outbox = tf.reshape(truth['rawbox'], shape)
        iou_score = iou(outbox_p, outbox)
        iou_score = tf.reduce_max(iou_score, axis=4, keepdims=True)
        is_obj_absent = tf.cast(iou_score <= self.iou_threshold, tf.int32)
        noobj_loss = (1 - truth['object']) * is_obj_absent
        noobj_loss *= prediction['object'] ** 2
        noobj_loss = self.noobject_scale * tf.reduce_sum(noobj_loss)

        return obj_loss + noobj_loss + coord_loss + class_loss
