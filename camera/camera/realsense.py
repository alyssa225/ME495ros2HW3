import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from sensor_msgs.msg import Image
from sensor_msgs.msg import CameraInfo
import numpy as np
from cv_bridge import CvBridge, CvBridgeError
import cv2
import sys
# np.set_printoptions(threshold=sys.maxsize)
import pyrealsense2 as rs2
from enum import Enum, auto

class State(Enum):
    """The current state of the brick."""

    # Waiting to get frames
    WAITING = auto(),
    # Initial scan to find the top of the tower
    FINDTOP = auto(),
    # Initial scan to find the bottom of the tower
    FINDTABLE = auto(),
    # Scanning to find a piece that's pushed out
    SCANNING = auto()

class Cam(Node):
    def __init__(self):
        super().__init__('cam')
        self.freq = 60.
        self.timer = self.create_timer(1./self.freq, self.timer_callback)

        self.color_sub = self.create_subscription(Image,
                                                  "/camera/color/image_raw",
                                                  self.color_callback,
                                                  10)
        self.depth_sub = self.create_subscription(Image,
                                                  "/camera/aligned_depth_to_color/image_raw",
                                                  self.depth_callback,
                                                  10)
        self.info_sub = self.create_subscription(CameraInfo,
                                                 "/camera/aligned_depth_to_color/camera_info",
                                                 self.info_callback,
                                                 10)
        self.br = CvBridge()

        self.color_frame = None
        self.depth_frame = None
        self.intrinsics = None

        self.band_start = 570
        self.band_width = 20

        kernel_size = 25
        self.kernel = np.ones((kernel_size,kernel_size),np.uint8)

        self.sq_orig = [523,0]
        self.sq_sz = 375

        self.rect = None
        self.update_rect()

        self.state = State.WAITING

        self.tower_top = None
        self.table = None
        self.scan_start = 100
        self.scan_index = self.scan_start
        self.max_scan = 800
        self.scan_step = 0.5

        # How large a contour is to consider it as relevant
        self.object_area_threshold = 10000
        self.piece_area_threshold = 2500

        self.piece_depth = 35 #33.782 #1.3 in

        cv2.namedWindow('Mask')
        cv2.createTrackbar('kernel size', 'Mask', kernel_size, 100, self.kernel_trackbar)
        cv2.createTrackbar('band width', 'Mask' , self.band_width, 100, self.band_width_tb)
        cv2.createTrackbar('band start', 'Mask' , self.band_start, 1000, self.band_start_tb)
        cv2.createTrackbar('origin x', 'Mask', self.sq_orig[0], 1000, self.sqx_trackbar)
        cv2.createTrackbar('origin y', 'Mask' , self.sq_orig[1], 1000, self.sqy_trackbar)
        cv2.createTrackbar('size', 'Mask' , self.sq_sz, 700, self.sqw_trackbar)

    def sqx_trackbar(self, val):
        self.sq_orig[0] = val
        self.update_rect()

    def sqy_trackbar(self, val):
        self.sq_orig[1] = val
        self.update_rect()

    def sqw_trackbar(self, val):
        self.sq_sz = val
        self.update_rect()

    def update_rect(self):
        self.rect = np.array([[self.sq_orig,
                               [self.sq_orig[0]+self.sq_sz, self.sq_orig[1]],
                               [self.sq_orig[0]+self.sq_sz, self.sq_orig[1]+self.sq_sz],
                               [self.sq_orig[0], self.sq_orig[1]+self.sq_sz]]],
                               dtype=np.int32)

    def band_width_tb(self, val):
        self.band_width = val

    def band_start_tb(self, val):
        self.band_start = val

    def kernel_trackbar(self, val):
        self.kernel = np.ones((val,val),np.uint8)

    def info_callback(self, cameraInfo):
        try:
            if self.intrinsics:
                return
            self.intrinsics = rs2.intrinsics()
            self.intrinsics.width = cameraInfo.width
            self.intrinsics.height = cameraInfo.height
            self.intrinsics.ppx = cameraInfo.k[2]
            self.intrinsics.ppy = cameraInfo.k[5]
            self.intrinsics.fx = cameraInfo.k[0]
            self.intrinsics.fy = cameraInfo.k[4]
            if cameraInfo.distortion_model == 'plumb_bob':
                self.intrinsics.model = rs2.distortion.brown_conrady
            elif cameraInfo.distortion_model == 'equidistant':
                self.intrinsics.model = rs2.distortion.kannala_brandt4
            self.intrinsics.coeffs = [i for i in cameraInfo.d]
        except CvBridgeError as e:
            self.get_logger().info("Getting intrinsics failed?")
            return

    def color_callback(self, data):
        color_frame = self.br.imgmsg_to_cv2(data, desired_encoding='bgr8')
        self.color_frame = color_frame

    def depth_callback(self, data):
        current_frame = self.br.imgmsg_to_cv2(data)
        self.depth_frame = current_frame

        # depth_colormap = cv2.applyColorMap(cv2.convertScaleAbs(current_frame, alpha=0.3), cv2.COLORMAP_JET)
        # cv2.imshow("im 13 and this is deep", depth_colormap)

    def get_mask(self):
        # Do this in case the subscriber somehow updates in the middle of the function?
        depth_cpy = np.array(self.depth_frame)

        # Only keep stuff that's within the appropriate depth band.
        depth_mask = cv2.inRange(depth_cpy, self.band_start, self.band_start+self.band_width)
        # This operation helps to remove "dots" on the depth image.
        # Kernel higher dimensional = smoother. It's also less important if camera is farther away.
        depth_mask = cv2.morphologyEx(depth_mask, cv2.MORPH_CLOSE, self.kernel)

        # All 0s, useful for following bitwise operations.
        bounding_mask = np.zeros((self.intrinsics.height,self.intrinsics.width), np.int8)
        # Creating a square over the area defined in self.rect
        square = cv2.fillPoly(bounding_mask, [self.rect], 255)
        # Blacking out everything that is not within square
        square = cv2.inRange(square, 1, 255)
        # Cropping the depth_mask so that only what is within the square remains.
        depth_mask = cv2.bitwise_and(depth_mask, depth_mask, mask=square)
        # Draw a rectangle on a copy of the depth_mask to visualize where it is.
        rectangle = cv2.rectangle(np.array(depth_mask),
                                  self.rect[0][0], self.rect[0][2],
                                  (255,0,0), 2)
        cv2.imshow("Mask", rectangle)

        # Find the contours of this cropped mask to help locate tower.
        contours, _ = cv2.findContours(depth_mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        centroids = []
        areas = []
        large_contours = []
        max_centroid = None
        # self.get_logger().info(f"Number of countours: {len(contours)}")
        for c in contours: 
            M = cv2.moments(c)
            area = cv2.contourArea(c)
            try: 
                cx = int(M['m10']/M['m00'])
                cy = int(M['m01']/M['m00'])
                centroid = (cx,cy)
                if area > 100:
                    centroids.append(centroid)
                    areas.append(area)
                    large_contours.append(c)
            except: 
                pass
        # self.get_logger().info(f"Areas: {areas}")
        largest_area = None
        if len(areas) != 0: 
            largest_index = np.argmax(areas)
            largest_area = areas[largest_index]
            max_centroid = centroids[largest_index]
            # self.get_logger().info(f"estimated center: {max_centroid}")
            centroid_depth = depth_cpy[max_centroid[1]][max_centroid[0]]
            # self.get_logger().info(f"Centroid depth: {centroid_depth}")
            deprojected = rs2.rs2_deproject_pixel_to_point(self.intrinsics,
                                                            [max_centroid[0], max_centroid[1]],
                                                            centroid_depth)
            # self.get_logger().info(f"DEPROJECTED Centroid depth:{deprojected}\n")


        color_copy = np.array(self.color_frame)
        drawn_contours = cv2.drawContours(color_copy, large_contours, -1, (0,255,0), 3)
        if max_centroid is not None:
            drawn_contours = cv2.circle(drawn_contours, max_centroid, 5, [0,0,255], 5)
        cv2.imshow("Depth Contours on Color Image", drawn_contours)

        cv2.waitKey(1)
        return largest_area

    def timer_callback(self):
        if self.state == State.WAITING:
            self.get_logger().info("Waiting for frames...")
            wait_for = [self.intrinsics, self.depth_frame, self.color_frame]
            if all(w is not None for w in wait_for):
                self.state = State.FINDTOP
                self.get_logger().info("Searching for tower top")

        elif self.state == State.FINDTOP:
            # Begin scanning downwards.
            self.band_start = self.scan_index
            self.scan_index += self.scan_step
            # Reset scan if too big
            if self.scan_index > self.max_scan:
                # This should not happen. But if it doesn't find anything large in the band:
                self.scan_index = self.scan_start
                self.get_logger().info("WTFFFFFFF")
                self.state = State.SCANNING

            largest_area = self.get_mask()
            if largest_area:
                if largest_area > self.object_area_threshold:
                    # We believe there is an object at this depth
                    self.get_logger().info("FOUND TOWER TOP!!!!!!")
                    self.get_logger().info(f"depth: {self.band_start},"+
                                           f"area: {largest_area}\n")
                    self.tower_top = self.band_start
                    # Go down past the top pieces, or else this will also be detected as table.
                    # (Need to increase by a bit more than 1x bandwidth. I do 1.5 to be safe.)
                    self.scan_index += 1.5*self.band_width
                    self.band_start += 1.5*self.band_width
                    # Go and find the table
                    self.state = State.FINDTABLE
                    self.get_logger().info("Searching for table")

        elif self.state == State.FINDTABLE:
            # Basically same logic as findtop.
            # Keep scanning downwards
            self.band_start = self.scan_index
            self.scan_index += self.scan_step
            # Reset scan if too big.
            if self.scan_index > self.max_scan:
                # This should not happen. But if it doesn't find anything large in the band:
                self.scan_index = self.scan_start
                self.get_logger().info("WTFFFFFFF")
                self.state = State.SCANNING

            largest_area = self.get_mask()
            if largest_area:
                if largest_area > self.object_area_threshold:
                    # We believe there is an object at this depth
                    self.get_logger().info("FOUND TABLE!!!!!!")
                    self.get_logger().info(f"depth: {self.band_start},"+
                                           f"area: {largest_area}\n")
                    self.table = self.band_start
                    self.scan_index = self.tower_top+1.5*self.band_width
                    self.band_start = self.tower_top+1.5*self.band_width
                    self.state = State.SCANNING
        elif self.state == State.SCANNING:
            # Keep scanning downwards
            self.band_start = self.scan_index
            self.scan_index += self.scan_step
            # Reset scan if too big.
            if self.scan_index+1.5*self.band_width > self.table:
                self.scan_index = self.tower_top +1.5*self.band_width
                self.get_logger().info("Reset scan")
            # Look for piece sticking out in range from top to table
            largest_area = self.get_mask()
            if largest_area:
                if largest_area > self.piece_area_threshold:
                    self.get_logger().info("I think there is a piece sticking out here")
                    self.get_logger().info(f"Index: {self.band_start}, area: {largest_area}")



def main(args=None):
    """Start and spin the node."""
    rclpy.init(args=args)
    c = Cam()
    rclpy.spin(c)
    rclpy.shutdown()


if __name__ == '__main__':
    main()