# Visual Loop Studio

Ứng dụng desktop local cho Windows để tạo visual nhạc 60 giây, kiểm tra kết quả, sau đó loop visual và mix audio thành video dài đúng bằng thời lượng main music.

## Tải EXE portable

Mở trang [GitHub Releases](https://github.com/duykhanh8386/tool-video-nhac/releases/latest) và tải `VisualLoopStudio-Windows-x64.exe`. EXE đã chứa Python, Qt, FFmpeg và FFprobe nên máy khác không cần cài Python hay FFmpeg.

Ứng dụng tự kiểm tra bản mới khi khởi động. Bạn cũng có thể bấm **Check for Updates** ở thanh bên hoặc menu **Help**. File cập nhật được kiểm tra SHA-256 trước khi thay thế EXE và khởi động lại.

## Cài đặt

Yêu cầu:

- Windows 10/11
- Python 3.11+
- FFmpeg và FFprobe có trong `PATH` (hoặc khai báo đường dẫn trong Settings)

Từ thư mục gốc, chạy `install.bat` một lần. Sau đó dùng `run.bat` để mở ứng dụng.

Hoặc chạy thủ công:

```powershell
cd visual_loop_studio
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

Ứng dụng dùng `PySide6-Essentials`, OpenCV headless và NumPy; toàn bộ xử lý media production vẫn chạy qua FFmpeg nên không cần Qt WebEngine/Multimedia.

## Workflow

1. Trong **Visual Creator — 60s**, chọn background, thêm text/logo/artwork/waveform, effect stack và color filter.
2. Chọn output folder rồi bấm **Render 1 Minute**. Output luôn là 60 giây và không ghi đè file cũ.
3. Kiểm tra bằng **Play Output**.
4. Sang **Loop Video + Music**, chọn visual vừa render và main music. Background music là tùy chọn.
5. Bấm **Analyze** để xem stream và thời lượng. Bấm **Start Render** để tạo final video.

Main music luôn là master clock. Video và background audio dùng FFmpeg streaming (`-stream_loop -1`), vì vậy không nạp toàn bộ video nhiều giờ vào RAM.

## Chức năng chính

- Hybrid Auto Layout với ba chế độ `AUTO`, `TEMPLATE`, `MANUAL`.
- Phân tích thumbnail background để lập bản đồ độ sáng/chi tiết/saliency; OpenCV tăng cường nhận diện mặt và người.
- Template chỉ là vùng ưu tiên. Engine vẫn tránh important region, kiểm tra va chạm và safe margin theo từng background.
- Mọi element dùng tọa độ chuẩn hóa `x/y/width/height`, cùng `anchor`, `rotation`, `opacity`, `z-order`, `locked`, `visible`.
- Kéo trực tiếp để di chuyển; kéo handle góc phải-dưới để resize; `Ctrl + mouse wheel` để rotate.
- Snap theo canvas center, safe margin, grid hoặc element khác; có alignment guides.
- `AUTO COMPOSE`, `TRY ANOTHER LAYOUT`, `RESET`, lock/hide từng element, duplicate và căn giữa.
- Hỗ trợ tiêu đề, phụ đề, nghệ sĩ, nội dung thêm, playlist nhiều dòng, logo, artwork, biểu tượng nền tảng và file sóng.
- Camera nền mặc định đứng yên. Có thể dùng ảnh nguồn + prompt để tạo video image-to-video bằng Veo 3.1; ảnh nguồn được dùng làm cả khung đầu và cuối nhằm ưu tiên vòng lặp liền mạch.
- File sóng PNG/GIF/MOV/MP4 chạy lặp theo thời gian riêng, không phản ứng theo âm lượng nhạc; có tùy chọn xóa nền trắng.
- Preview nhẹ 16:9 cho image/video background, artwork, logo, text, waveform, color preset và nhiều full-frame effect.
- Render visual đúng 60 giây ở 1080p/4K, 24–60 fps.
- Effect stack có add/remove/reorder/enable/intensity; mặc định rỗng.
- Color filter và `.cube` LUT; mặc định `NONE`.
- Dò media bằng FFprobe theo stream thực, không chỉ dựa vào đuôi file.
- Auto NVENC khi GPU hoạt động; fallback libx264.
- Progress, tốc độ, FPS, ETA, dung lượng output; cancel không khóa GUI.
- Audio Mixer sáu track; thời lượng theo main audio.
- Batch queue hỗ trợ 1–3 job đồng thời, mặc định 1 để ổn định NVENC/RAM.
- Lưu/mở project `.vls.json`; nhớ settings và output folder.
- Log render trong `visual_loop_studio/logs/`.
- Gemini/Veo API và ComfyUI đều là tích hợp tùy chọn; chức năng render lõi không cần AI. Nhập Gemini API key trong Cài đặt hoặc biến môi trường `GEMINI_API_KEY` để gọi Veo.

## Kiểm thử

```powershell
cd visual_loop_studio
python -m unittest discover -s tests -v
```

Các test bao phủ FFprobe parser, master duration, loop command, effect defaults/order/seam, và project round-trip.

## Build và phát hành tự động

Workflow `.github/workflows/build-release.yml` chạy trên Windows sau mỗi push vào nhánh `main`:

1. Cài dependency và chạy test.
2. Đóng gói một EXE portable bằng PyInstaller.
3. Nhúng FFmpeg và FFprobe vào EXE.
4. Tạo file SHA-256.
5. Tạo version mới dạng `1.0.<run>.<attempt>`.
6. Đăng EXE lên GitHub Releases và đánh dấu là bản mới nhất.

Để build thủ công trên Windows, chạy `build_windows.ps1` từ thư mục gốc.

## Ghi chú

- Các hiệu ứng hạt phức tạp được preview theo kiểu procedural nhẹ; renderer FFmpeg dùng các xấp xỉ streamable để giữ RAM ổn định.
- Background crossfade hiện dùng fade mềm ở đầu stream loop. Không tạo file audio lặp thủ công.
- Nếu encoder GPU báo lỗi, chọn `Auto` hoặc `libx264`.
