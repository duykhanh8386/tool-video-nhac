# Visual Loop Studio

Ứng dụng Windows tạo video chuyển động, loop video theo nhạc, trộn âm thanh và render hàng loạt bằng FFmpeg.

## Tải bản EXE portable

Tải hai file trong [GitHub Releases](https://github.com/duykhanh8386/tool-video-nhac/releases/latest):

- `VisualLoopStudio-Windows-x64.exe`
- `VisualLoopStudio-Windows-x64.exe.sha256` để kiểm tra tính toàn vẹn

Bản EXE đã đóng gói Python, Qt, FFmpeg và FFprobe; không cần cài Python hoặc FFmpeg trên máy đích. Đây là bản portable, chỉ cần tải về rồi chạy. Windows SmartScreen có thể cảnh báo vì file chưa được ký bằng chứng thư số thương mại.

## Cập nhật

Ứng dụng tự kiểm tra bản phát hành mới khi khởi động. Bạn cũng có thể bấm **Check for Updates** ở thanh bên hoặc menu **Help**. Bản cập nhật chỉ được cài sau khi SHA-256 khớp với checksum trên GitHub Release.

FFmpeg, FFprobe, kiểm tra GPU và helper cập nhật đều chạy ẩn trong nền. Không có cửa sổ CMD bật/tắt nhấp nháy; output render được gom vào trạng thái trong ứng dụng và file log.

## Chuyển động AI và file sóng

- Ảnh nền mặc định dùng camera cố định, không còn tự zoom/pan gây rung toàn khung.
- Có chế độ **Wan 2.2 TI2V 5B Native** chạy local qua ComfyUI: không token, không API key, không credit. Mã nguồn/model dùng giấy phép Apache 2.0.
- Ngay trong phần **Nguồn ảnh AI**, có thể chọn một ảnh, chọn nhiều ảnh riêng lẻ hoặc chọn cả thư mục (kể cả thư mục con). Một prompt dùng chung sẽ tạo clip AI cho từng ảnh theo hàng đợi, đưa từng clip vào cùng bố cục, rồi xuất đủ số video.
- Có thể nhập prompt và dùng Veo 3.1 để tạo chuyển động tay/chân, người, khói, lửa hoặc ánh sáng từ ảnh nguồn. Cần Gemini API key và tài khoản có quyền dùng Veo.
- File sóng dạng PNG/GIF/MOV/MP4 chạy lặp độc lập, không co giãn theo âm lượng nhạc; có tùy chọn xóa nền trắng.
- Có input **Overlay toàn cảnh** cho PNG/GIF/MOV/MP4. File động phát và tự lặp theo thời lượng gốc trong cả preview lẫn video xuất; hỗ trợ Normal, Lighten, Screen, Linear Dodge (Add) và độ mờ.
- Preview MOV được giải mã thành RGBA để giữ kênh alpha. File sóng và overlay toàn cảnh đều có tùy chọn xóa nền trắng nếu video nguồn đã bị đóng nền trắng thay vì có alpha thật.
- Preview giải mã frame thật của video nền, file sóng và overlay thay vì chỉ giữ frame đầu. Hiệu ứng toàn khung tích hợp cũng hiển thị theo cường độ đã chọn.
- Veo tạo video 24 fps nên ứng dụng tự chuyển FPS project về 24 sau khi tạo để giữ nhịp khung hình mượt.

## Thiết lập Wan 2.2 local cho RTX 3060

EXE của Visual Loop Studio là portable; không cần chạy `install.bat` hay `run.bat`. Ở lần dùng AI Local đầu tiên, bấm **Cài AI Local tự động (chỉ lần đầu)**, chọn ổ đĩa còn tối thiểu 32 GB trống và xác nhận. Tool sẽ tự tải bản [ComfyUI Portable NVIDIA chính thức](https://github.com/Comfy-Org/ComfyUI/releases) cùng ba model theo [hướng dẫn Wan 2.2 chính thức](https://docs.comfy.org/tutorials/video/wan/wan2_2):

- `wan2.2_ti2v_5B_fp16.safetensors` → `ComfyUI/models/diffusion_models/`
- `umt5_xxl_fp8_e4m3fn_scaled.safetensors` → `ComfyUI/models/text_encoders/`
- `wan2.2_vae.safetensors` → `ComfyUI/models/vae/`

Sau khi cài xong, Visual Loop Studio tự khởi động ComfyUI ẩn ở `http://127.0.0.1:8188` khi mở app và tự tắt tiến trình do app mở khi thoát. Không có cửa sổ CMD nhấp nháy; log nằm trong `VisualLoopStudio/logs/comfyui.log`. File tải dở được giữ để tiếp tục nếu mạng bị ngắt. Dùng preset `720p 1280×704 — RTX 3060`; các video được chạy tuần tự để hạn chế tràn VRAM.

Gói ComfyUI Portable được giải nén bằng `7zr.exe` chính thức của 7-Zip để hỗ trợ bộ lọc BCJ2; file 7zr được kiểm tra SHA-256 trước khi chạy. Nếu bản cũ đã tải xong archive ComfyUI nhưng lỗi ở bước BCJ2, bản mới sẽ dùng lại archive đó thay vì tải lại gần 2 GB.

## Chỉnh element và render hàng loạt

- Click vào Logo, Ảnh bìa, Icon nền tảng hoặc Sóng trên preview để mở ngay hộp chọn file; kéo chuột vẫn dùng để di chuyển và kéo góc để đổi kích thước.
- Mỗi thành phần ảnh có chế độ hòa trộn `Normal`, `Lighten`, `Screen` hoặc `Linear Dodge (Add)` trong phần **Bố cục thông minh**.
- Mỗi loại text có font, cỡ chữ, đậm và nghiêng riêng. Danh sách font lấy trực tiếp từ font đã cài trên Windows giống bộ chọn font trong Word.
- Cỡ chữ dùng chung một thang thiết kế 1080p nên preview, video 1080p và video 4K giữ đúng tỷ lệ.
- Có thể chọn **Thư mục background để render hàng loạt** rồi bấm một lần để xếp hàng toàn bộ ảnh/video trong thư mục; mỗi background tạo một video riêng với cùng thiết kế hiện tại.

Mỗi lần có commit mới được push lên nhánh `main`, GitHub Actions sẽ:

1. Chạy toàn bộ kiểm thử.
2. Đóng gói EXE portable kèm FFmpeg/FFprobe.
3. Tạo checksum SHA-256.
4. Tạo GitHub Release mới và đánh dấu là bản mới nhất.

## Chạy từ mã nguồn

```powershell
.\install.bat
.\run.bat
```

## Tự build trên Windows

Cần có Python 3.12 và bản FFmpeg đầy đủ trong `PATH` (không chỉ Chocolatey shim):

```powershell
.\build_windows.ps1
```

File kết quả nằm tại `dist\VisualLoopStudio-Windows-x64.exe`. Tài liệu chi tiết hơn nằm trong [`visual_loop_studio/README.md`](visual_loop_studio/README.md).
