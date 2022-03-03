import queue
import socket
import struct
import sys
import time
import threading
import pickle

import numpy as np
import rclpy
from rclpy.node import Node
from angel_msgs.msg import (
    ObjectDetection2dSet,
    ObjectDetection3dSet,
    SpatialMesh,
    HeadsetPoseData
)
from angel_utils.conversion import to_confidence_matrix
from geometry_msgs.msg import Point

import trimesh
import trimesh.viewer


class SpatialMapSubscriber(Node):

    def __init__(self):
        super().__init__(self.__class__.__name__)

        self.declare_parameter("spatial_map_topic", "SpatialMapData")
        self.declare_parameter("det_topic", "ObjectDetections")
        self.declare_parameter("3d_det_topic", "ObjectDetections3d")
        self.declare_parameter("pose_topic", "HeadsetPoseData")

        self._spatial_map_topic = self.get_parameter("spatial_map_topic").get_parameter_value().string_value
        self._det_topic = self.get_parameter("det_topic").get_parameter_value().string_value
        self._3d_det_topic = self.get_parameter("3d_det_topic").get_parameter_value().string_value
        self._pose_topic = self.get_parameter("pose_topic").get_parameter_value().string_value

        log = self.get_logger()
        log.info(f"Spatial map topic: {self._spatial_map_topic}")
        log.info(f"Detection topic: {self._det_topic}")
        log.info(f"Pose topic: {self._pose_topic}")

        self._spatial_mesh_subscription = self.create_subscription(
            SpatialMesh,
            self._spatial_map_topic,
            self.spatial_map_callback,
            100)

        self._detection_subscription = self.create_subscription(
            ObjectDetection2dSet,
            self._det_topic,
            self.detection_callback,
            100
        )

        self._pose_subscription = self.create_subscription(
            HeadsetPoseData,
            self._pose_topic,
            self.headset_pose_callback,
            100
        )

        self._object_3d_publisher = self.create_publisher(
            ObjectDetection3dSet,
            self._3d_det_topic,
            1
        )

        self.frames_recvd = 0
        self.prev_time = -1
        self.meshes = {}

        self.scene = trimesh.Scene()

        self.poses = []

        log = self.get_logger()
        #log.set_level(10)


    def spatial_map_callback(self, msg):
        log = self.get_logger()
        self.frames_recvd += 1

        # extract the vertices into np arrays
        vertices = np.array([])
        for v in msg.mesh.vertices:
            if vertices.size == 0:
                vertices = np.array([v.x, v.y, v.z])
            else:
                vertices = np.vstack([vertices, np.array([v.x, v.y, v.z])])

        # extract the triangles into np arrays
        triangles = np.array([])
        for t in msg.mesh.triangles:
            if triangles.size == 0:
                triangles = np.array([t.vertex_indices[0],
                                      t.vertex_indices[1],
                                      t.vertex_indices[2]
                                     ])
            else:
                triangles = np.vstack([triangles,
                                       np.array([t.vertex_indices[0],
                                                 t.vertex_indices[1],
                                                 t.vertex_indices[2]
                                                ])])

        if msg.removal:
            log.debug("Got a removal!")
            # TODO: remove from the spatial map

        mesh = trimesh.Trimesh(vertices=vertices, faces=triangles)
        self.meshes[msg.mesh_id] = mesh
        self.scene.add_geometry(mesh)


    def headset_pose_callback(self, pose):
        log = self.get_logger()

        #log.info(f"pose stamp: {pose.header.stamp}")

        self.poses.append(pose)


    def detection_callback(self, detection):
        log = self.get_logger()

        if detection.num_detections == 0:
            log.debug("No detections for this image")
            return

        world_matrix_1d = None
        projection_matrix_1d = None

        for i in range(len(self.poses)):
            if detection.source_stamp == self.poses[i].header.stamp:
                world_matrix_1d = self.poses[i].world_matrix
                projection_matrix_1d = self.poses[i].projection_matrix

                # can clear out our pose list now assuming we won't get
                # image frame detections out of order
                self.poses = self.poses[i:]
                break

        if world_matrix_1d == None or projection_matrix_1d == None:
            log.info("Did not get world or projection matrix")
            log.debug(f"image stamp: {detection.source_stamp}")
            for i in range(len(self.poses)):
                log.info(f"pose stamp: {self.poses[i].header.stamp}")
            return

        # get world matrix from detection
        world_matrix_2d = self.convert_1d_4x4_to_2d_matrix(world_matrix_1d)

        # negate z component of world matrix
        world_matrix_2d[0][2] = -world_matrix_2d[0][2]
        world_matrix_2d[1][2] = -world_matrix_2d[1][2]
        world_matrix_2d[2][2] = -world_matrix_2d[2][2]
        log.debug(str(world_matrix_2d))

        # get projection matrix from detection
        projection_matrix_2d = self.convert_1d_4x4_to_2d_matrix(projection_matrix_1d)

        # get the inverse of the projection matrix
        projection_inv = np.linalg.inv(projection_matrix_2d)

        # get position of the camera at the time of the frame
        camera_origin = self.get_world_position(world_matrix_2d,
                                                np.array([0.0, 0.0, 0.0]))
        camera_origin = camera_origin.reshape((1, 3))
        #log.debug(f"origin: {camejra_origin} {camera_origin.shape}")

        det_conf_mat = to_confidence_matrix(detection)

        det_3d_set_msg = ObjectDetection3dSet()
        det_3d_set_msg.header.stamp = self.get_clock().now().to_msg()
        det_3d_set_msg.header.frame_id = detection.header.frame_id
        det_3d_set_msg.source_stamp = detection.source_stamp

        for i in range(detection.num_detections):
            object_type = sorted(zip(det_conf_mat[i], detection.label_vec))[-1][1]

            # get pixel positions of detected object box and form into box corners
            min_vertex0 = detection.left[i]
            min_vertex1 = detection.top[i]
            max_vertex0 = detection.right[i]
            max_vertex1 = detection.bottom[i]

            corners_screen_pos = [[min_vertex0, min_vertex1], [min_vertex0, max_vertex1],
                                  [max_vertex0, max_vertex1], [max_vertex0, min_vertex1]]

            # convert detection screen pixel coordinates to world coordinates
            corners_world_pos = []
            for p in corners_screen_pos:
                point_3d = self.convert_2d_coord_to_3d_coord(world_matrix_2d,
                                                             projection_inv,
                                                             p, camera_origin)
                if point_3d is None:
                    return
                corners_world_pos.append(point_3d)

            log.info(f"Drawing box for {object_type}")

            vs = np.array([corners_world_pos[0], corners_world_pos[1],
                           corners_world_pos[2], corners_world_pos[3]])
            el = trimesh.path.entities.Line([0, 1, 2, 3, 0])
            path = trimesh.path.Path3D(entities=[el], vertices=vs)
            self.scene.add_geometry(path)

            # since we were able to find this object's 3D position,
            # add it to 3d detection message
            det_3d_set_msg.object_labels.append(object_type)
            det_3d_set_msg.num_objects += 1

            for p in range(4):
                point_3d = Point()
                point_3d.x = corners_world_pos[p][0]
                point_3d.y = corners_world_pos[p][1]
                point_3d.z = corners_world_pos[p][2]

                if p == 0:
                    det_3d_set_msg.left.append(point_3d)
                elif p == 1:
                    det_3d_set_msg.top.append(point_3d)
                elif p == 2:
                    det_3d_set_msg.right.append(point_3d)
                elif p == 3:
                    det_3d_set_msg.bottom.append(point_3d)

        # form and publish the 3d object detection message
        self._object_3d_publisher.publish(det_3d_set_msg)

        # uncomment this to visualize the scene
        #self.show_plot()


    def show_plot(self):
        try:
            self.scene.show()
        except:
            pass


    def cast_ray(self, origin, direction):
        intersection_point = None
        for key, m in self.meshes.items():
            ray_intersector = trimesh.ray.ray_triangle.RayMeshIntersector(m)
            intersection = ray_intersector.intersects_location(origin, direction)

            if (len(intersection[0])) != 0:
                intersection_point = intersection[0]

        return intersection_point


    def get_world_position(self, world_matrix, point):
        point_matrix = np.array([[point[0]], [point[1]], [point[2]], [1]])
        return np.matmul(world_matrix, point_matrix)[:3]


    def convert_1d_4x4_to_2d_matrix(self, matrix_1d):
        matrix_2d = [[], [], [], []]
        for row in range(4):
            for col in range(4):
                idx = row * 4 + col
                matrix_2d[row].append(matrix_1d[idx])

        return matrix_2d


    def convert_2d_coord_to_3d_coord(self, world_matrix, projection_matrix_inv,
                                     point, camera_origin):
        log = self.get_logger()

        # TODO: don't hardcode these
        image_width = 1280.0
        image_height = 720.0

        # scale by image width and height and convert to -1:1 coordinates
        image_pos_zero_to_one = np.array([point[0] / image_width, 1 - (point[1] / image_height)])
        image_pos_zero_to_one = (image_pos_zero_to_one * 2) - np.array([1, 1])

        # convert screen point to camera point
        image_pos_projected = self.get_world_position(projection_matrix_inv,
                                                      np.array([image_pos_zero_to_one[0],
                                                                image_pos_zero_to_one[1],
                                                                1]))
        #print("object position in camera space 1", image_pos_projected)

        # convert camera position to world position
        world_space_box_pos = self.get_world_position(world_matrix,
                                                      np.array([image_pos_projected[0][0],
                                                                image_pos_projected[1][0],
                                                                1]))
        world_space_box_pos = world_space_box_pos.reshape((1, 3))
        #print("object position in world space ", world_space_box_pos, world_space_box_pos.shape)

        # cast ray from camera origin to the object
        intersecting_points = self.cast_ray(camera_origin,
                                            world_space_box_pos - camera_origin)
        if intersecting_points is None:
            log.info("No intersecting meshes found!")
            return None

        closest_point = None
        try:
            #log.debug(f"Points found {intersecting_points}, {object_type}")

            # if there is more than one point found, use the closest one to the camera
            min_distance = -1
            for p in intersecting_points:
                # calculate distance between this point and the camera
                distance = ((camera_origin[0][0] - p[0]) ** 2 +
                            (camera_origin[0][1] - p[1]) ** 2 +
                            (camera_origin[0][2] - p[2]) ** 2) ** 0.5
                #log.debug(f"Point {p}: distance = {distance}")
                if min_distance == -1:
                    min_distance = distance
                    closest_point = p
                elif distance < min_distance:
                    min_distance = distance
                    closest_point = p

            #log.debug(f"Closest point = {closest_point}")
        except Exception as e:
            log.info(e)

        return closest_point


def main():
    rclpy.init()

    spatial_map_subscriber = SpatialMapSubscriber()
    rclpy.spin(spatial_map_subscriber)

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    spatial_map_subscriber.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
