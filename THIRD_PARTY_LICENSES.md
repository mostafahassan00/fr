# Third-party components

Face Sorter itself is MIT-licensed (see `LICENSE`). The portable Windows build also
contains the components below, each under its own license. All of them allow free
redistribution; please keep this file with any copy you share.

| Component | Used for | License | Source |
|---|---|---|---|
| Python 3.12 | runs the program | PSF License | https://www.python.org/ |
| Tcl/Tk (with Python) | the window | Tcl/Tk License (BSD-style) | https://www.tcl.tk/ |
| OpenCV (opencv-python-headless) | image reading, face detection/recognition runtime | Apache-2.0 | https://opencv.org/ |
| YuNet face detection model (`face_detection_yunet_2023mar.onnx`) | finding faces | MIT | https://github.com/opencv/opencv_zoo |
| SFace face recognition model (`face_recognition_sface_2021dec.onnx`) | face prints | Apache-2.0 | https://github.com/opencv/opencv_zoo |
| NumPy | numbers | BSD-3-Clause | https://numpy.org/ |
| psutil | battery / priority | BSD-3-Clause | https://github.com/giampaolo/psutil |
| sqlcipher3-wheels (SQLCipher Community Edition) | encrypted library file | BSD-style (SQLCipher) / zlib (sqlcipher3) | https://www.zetetic.net/sqlcipher/ |
| tkinterdnd2 (with tkdnd) | drag and drop | MIT (tkinterdnd2), BSD-style (tkdnd) | https://github.com/Eliav2/tkinterdnd2 |

The full license texts of these components are included in their folders inside
`FaceSorter/python` (Python, Lib/site-packages/*/LICENSE*).
