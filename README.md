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
- Có bốn chế độ AI local qua ComfyUI: **Wan Chất lượng 20 bước**, **Wan DMD 4 bước**, **LTX-Video 2B Distilled 8 bước** và **HunyuanVideo 1.5 480p 8 bước**. Tất cả render bằng máy hiện tại, không dùng token/API key/credit.
- Ngay trong phần **Nguồn ảnh AI**, có thể chọn một ảnh, chọn nhiều ảnh riêng lẻ hoặc chọn cả thư mục (kể cả thư mục con). Một prompt dùng chung sẽ tạo clip AI cho từng ảnh theo hàng đợi, đưa từng clip vào cùng bố cục, rồi xuất đủ số video.
- Có thể nhập prompt và dùng Veo 3.1 để tạo chuyển động tay/chân, người, khói, lửa hoặc ánh sáng từ ảnh nguồn. Cần Gemini API key và tài khoản có quyền dùng Veo.
- File sóng dạng PNG/GIF/MOV/MP4 chạy lặp độc lập, không co giãn theo âm lượng nhạc; có tùy chọn xóa nền trắng.
- Có input **Overlay toàn cảnh** cho PNG/GIF/MOV/MP4. File động phát và tự lặp theo thời lượng gốc trong cả preview lẫn video xuất; hỗ trợ Normal, Lighten, Screen, Linear Dodge (Add) và độ mờ.
- Preview MOV được giải mã thành RGBA để giữ kênh alpha. File sóng và overlay toàn cảnh đều có tùy chọn xóa nền trắng nếu video nguồn đã bị đóng nền trắng thay vì có alpha thật.
- Preview giải mã frame thật của video nền, file sóng và overlay thay vì chỉ giữ frame đầu. Hiệu ứng toàn khung tích hợp cũng hiển thị theo cường độ đã chọn.
- Veo tạo video 24 fps nên ứng dụng tự chuyển FPS project về 24 sau khi tạo để giữ nhịp khung hình mượt.

## Thiết lập AI Video local đa model

EXE của Visual Loop Studio là portable; không cần chạy `install.bat` hay `run.bat`. Ở lần dùng AI Local đầu tiên, chọn model rồi bấm nút cài/tải. Tool tính dung lượng còn thiếu, tự nhận diện GPU và tải đúng bản [ComfyUI Portable chính thức](https://github.com/Comfy-Org/ComfyUI/releases): NVIDIA CUDA, Intel Arc XPU, AMD hoặc CPU. Mỗi máy tự tải model và dùng GPU của chính máy đó.

- **Chất lượng 20 bước:** `wan2.2_ti2v_5B_fp16.safetensors` (Apache 2.0) → `ComfyUI/models/diffusion_models/`
- **DMD 4 bước:** `wan2.2_5b_nonar_dmd_4step_lora_r64_comfy.safetensors` (Apache 2.0, khoảng 645 MB) → `ComfyUI/models/loras/`
- `umt5_xxl_fp8_e4m3fn_scaled.safetensors` → `ComfyUI/models/text_encoders/`
- `wan2.2_vae.safetensors` → `ComfyUI/models/vae/`
- **LTX-Video 2B Distilled 8 bước:** checkpoint 0.9.6 và T5 FP8, tổng khoảng 11,5 GB; khuyến nghị `832×480` để farm nhanh.
- **HunyuanVideo 1.5 I2V Step Distilled 8 bước:** model FP8 480p cùng Qwen/ByT5/VAE/SigCLIP, tổng khoảng 21,5 GB; trong tool chỉ cho NVIDIA CUDA và khuyến nghị từ 16 GB VRAM.

LoRA gốc dùng key PEFT. Tool kiểm tra SHA-256, tự đổi 600 tên tensor sang key native của ComfyUI mà không thay đổi dữ liệu trọng số, rồi mới cho phép chạy workflow 4 bước.

Sau khi cài xong, Visual Loop Studio tự khởi động ComfyUI ẩn ở `http://127.0.0.1:8188` khi mở app và tự tắt tiến trình do app mở khi thoát. Không có cửa sổ CMD nhấp nháy; log nằm trong `VisualLoopStudio/logs/comfyui.log`. File tải dở được giữ để tiếp tục nếu mạng bị ngắt. Các video được chạy tuần tự để hạn chế tràn VRAM.

Wan và Wan DMD dùng Apache 2.0. [LTX-Video Open Weights License](https://huggingface.co/Lightricks/LTX-Video/blob/main/LTX-Video-Open-Weights-License-0.X.txt) cho phép thương mại miễn phí dưới ngưỡng doanh thu năm nêu trong giấy phép và yêu cầu đánh dấu nội dung AI khi công bố. [HunyuanVideo 1.5 Community License](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5/blob/main/LICENSE) có điều kiện lãnh thổ/phân phối và yêu cầu đánh dấu nội dung AI; cần kiểm tra điều khoản trước khi dùng thương mại quốc tế.

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
