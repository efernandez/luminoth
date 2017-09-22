import sonnet as snt
import tensorflow as tf

from luminoth.utils.bbox_transform_tf import decode, clip_boxes, change_order


class RCNNProposal(snt.AbstractModule):
    """Create final object detection proposals.

    RCNNProposals takes the proposals generated by the RPN and the predictions
    of the RCNN (both classification and boundin box adjusting) and generates
    a list of object proposals with assigned class.

    In the process it tries to remove duplicated suggestions by applying non
    maximum suppresion (NMS).

    We apply NMS because the way object detectors are usually scored is by
    treating duplicated detections (multiple detections that overlap the same
    ground truth value) as false positive. It is resonable to assume that there
    may exist such case that applying NMS is completly unnecesary.

    Besides applying NMS it also filters the top N results, both for classes
    and in general. These values are easily modifiable in the configuration
    files.
    """
    def __init__(self, num_classes, config, name='rcnn_proposal'):
        """
        Args:
            num_classes: Total number of classes RCNN is classifying.
            config: Configuration object.
        """
        super(RCNNProposal, self).__init__(name=name)
        self._num_classes = num_classes

        # Max number of object detections per class.
        self._class_max_detections = config.class_max_detections
        # NMS intersection over union threshold to be used for classes.
        self._class_nms_threshold = config.class_nms_threshold
        # Maximum number of detections to return.
        self._total_max_detections = config.total_max_detections
        # Threshold probability
        self._min_prob_threshold = config.min_prob_threshold or 0.0

    def _build(self, proposals, bbox_pred, cls_prob, im_shape):
        """
        Args:
            proposals: Tensor with the RPN proposals bounding boxes.
                Shape (num_proposals, 5). Where num_proposals is less than
                POST_NMS_TOP_N (We don't know exactly beforehand)
            bbox_pred: Tensor with the RCNN delta predictions for each proposal
                for each class. Shape (num_proposals, 4 * num_classes)
            cls_prob: A softmax probability for each proposal where the idx = 0
                is the background class (which we should ignore).
                Shape (num_proposals, num_classes + 1)

        Returns:
            objects:
                Shape (final_num_proposals, 4)
                Where final_num_proposals is unknown before-hand (it depends on
                NMS). The 4-length Tensor for each corresponds to:
                (x_min, y_min, x_max, y_max).
            objects_label:
                Shape (final_num_proposals,)
            objects_label_prob:
                Shape (final_num_proposals,)

        """

        # remove batch_id from proposals
        with tf.control_dependencies([tf.equal(tf.shape(proposals)[-1], 5)]):
            proposals = proposals[:, 1:]

        # First we want get the most probable label for each proposal
        # We still have the background on idx 0 so we substract 1 to the idxs.
        proposal_label = tf.argmax(cls_prob, axis=1) - 1
        # Get the probability for the selected label for each proposal.
        proposal_label_prob = tf.reduce_max(cls_prob, axis=1)

        # We are going to use only the non-background proposals.
        non_background_filter = tf.greater_equal(proposal_label, 0)
        # Filter proposals with less than threshold probability.
        min_prob_filter = tf.greater_equal(
            proposal_label_prob, self._min_prob_threshold
        )
        proposal_filter = tf.logical_and(
            non_background_filter, min_prob_filter
        )

        equal_shapes = tf.assert_equal(
            tf.shape(proposals)[0], tf.shape(bbox_pred)[0]
        )
        with tf.control_dependencies([equal_shapes]):
            # Filter all tensors for getting all non-background proposals.
            proposals = tf.boolean_mask(
                proposals, proposal_filter)
            proposal_label = tf.boolean_mask(
                proposal_label, proposal_filter)
            proposal_label_prob = tf.boolean_mask(
                proposal_label_prob, proposal_filter)
            bbox_pred = tf.boolean_mask(
                bbox_pred, proposal_filter)

        # Create one hot with labels for using it to filter bbox_predictions.
        label_one_hot = tf.one_hot(proposal_label, depth=self._num_classes)
        # Flatten label_one_hot to get
        # (num_non_background_proposals * num_classes, 1) for filtering.
        label_one_hot_flatten = tf.cast(
            tf.reshape(label_one_hot, [-1]), tf.bool
        )
        # Flatten bbox_predictions getting
        # (num_non_background_proposals * num_classes, 4).
        bbox_pred_flatten = tf.reshape(bbox_pred, [-1, 4])

        equal_shapes = tf.assert_equal(
            tf.shape(bbox_pred_flatten)[0], tf.shape(label_one_hot_flatten)[0]
        )
        with tf.control_dependencies([equal_shapes]):
            # Control same number of dimensions between bbox and mask.
            bbox_pred = tf.boolean_mask(
                bbox_pred_flatten, label_one_hot_flatten)

        # Using the bbox_pred and the proposals we generate the objects.
        raw_objects = decode(proposals, bbox_pred)
        # Clip boxes to image.
        clipped_objects = clip_boxes(raw_objects, im_shape)

        # Filter objects that have an non-valid area.
        (x_min, y_min, x_max, y_max) = tf.unstack(clipped_objects, axis=1)
        object_filter = tf.greater_equal(
            tf.maximum(x_max - x_min, 0.0) * tf.maximum(y_max - y_min, 0.0),
            0.0
        )

        objects = tf.boolean_mask(
            clipped_objects, object_filter)
        proposal_label = tf.boolean_mask(
            proposal_label, object_filter)
        proposal_label_prob = tf.boolean_mask(
            proposal_label_prob, object_filter)

        # We have to use the TensorFlow's bounding box convention to use the
        # included function for NMS.
        # After gathering results we should normalize it back.
        objects_tf = change_order(objects)

        selected_boxes = []
        selected_probs = []
        selected_labels = []
        # For each class we want to filter those objects and apply NMS to them.
        for class_id in range(self._num_classes):
            # Filter objects Tensors with class.
            class_filter = tf.equal(proposal_label, class_id)
            class_objects_tf = tf.boolean_mask(objects_tf, class_filter)
            class_prob = tf.boolean_mask(proposal_label_prob, class_filter)

            # Apply class NMS.
            class_selected_idx = tf.image.non_max_suppression(
                class_objects_tf, class_prob, self._class_max_detections,
                iou_threshold=self._class_nms_threshold
            )

            # Using NMS resulting indices, gather values from Tensors.
            class_objects_tf = tf.gather(class_objects_tf, class_selected_idx)
            class_prob = tf.gather(class_prob, class_selected_idx)

            # We append values to a regular list which will later be transform
            # to a proper Tensor.
            selected_boxes.append(class_objects_tf)
            selected_probs.append(class_prob)
            # In the case of the class_id, since it is a loop on classes, we
            # already have a fixed class_id. We use `tf.tile` to create that
            # Tensor with the total number of indices returned by the NMS.
            selected_labels.append(
                tf.tile([class_id], [tf.shape(class_selected_idx)[0]])
            )

        # We use concat (axis=0) to generate a Tensor where the rows are
        # stacked on top of each other
        objects_tf = tf.concat(selected_boxes, axis=0)
        # Return to the original convention.
        objects = change_order(objects_tf)
        proposal_label = tf.concat(selected_labels, axis=0)
        proposal_label_prob = tf.concat(selected_probs, axis=0)

        # Get topK detections of all classes.
        k = tf.minimum(
            self._total_max_detections,
            tf.shape(proposal_label_prob)[0]
        )
        top_k = tf.nn.top_k(proposal_label_prob, k=k)
        top_k_proposal_label_prob = top_k.values
        top_k_objects = tf.gather(objects, top_k.indices)
        top_k_proposal_label = tf.gather(proposal_label, top_k.indices)

        return {
            'raw_objects': raw_objects,
            'objects': top_k_objects,
            'proposal_label': top_k_proposal_label,
            'proposal_label_prob': top_k_proposal_label_prob,
            'selected_boxes': selected_boxes,
            'selected_probs': selected_probs,
            'selected_labels': selected_labels,
        }
