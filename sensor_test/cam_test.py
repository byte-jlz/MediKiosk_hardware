import cv2

QR_CAM = "/dev/v4l/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1:1.0-video-index0"
BP_CAM = "/dev/v4l/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-video-index0"

qr_cam = cv2.VideoCapture(QR_CAM, cv2.CAP_V4L2)
bp_cam = cv2.VideoCapture(BP_CAM, cv2.CAP_V4L2)

for cam in (qr_cam, bp_cam):
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

while True:
    ok_qr, frame_qr = qr_cam.read()
    ok_bp, frame_bp = bp_cam.read()

    if ok_qr:
        cv2.imshow("QR Cam", frame_qr)
    if ok_bp:
        cv2.imshow("BP Cam", frame_bp)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

qr_cam.release()
bp_cam.release()
cv2.destroyAllWindows()
