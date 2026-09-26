# Visual Loop Studio

Ứng dụng Windows tạo video chuyển động, loop video theo nhạc, trộn âm thanh và render hàng loạt bằng FFmpeg.

## Tải bản EXE portable

Tải hai file trong [GitHub Releases](https://github.com/duykhanh8386/tool-video-nhac/releases/latest):

- `VisualLoopStudio-Windows-x64.exe`
- `VisualLoopStudio-Windows-x64.exe.sha256` để kiểm tra tính toàn vẹn

Bản EXE đã đóng gói Python, Qt, FFmpeg và FFprobe; không cần cài Python hoặc FFmpeg trên máy đích. Đây là bản portable, chỉ cần tải về rồi chạy. Windows SmartScreen có thể cảnh báo vì file chưa được ký bằng chứng thư số thương mại.

## Cập nhật

Ứng dụng tự kiểm tra bản phát hành mới khi khởi động. Bạn cũng có thể bấm **Check for Updates** ở thanh bên hoặc menu **Help**. Bản cập nhật chỉ được cài sau khi SHA-256 khớp với checksum trên GitHub Release.

## Chuyển động AI và file sóng

- Ảnh nền mặc định dùng camera cố định, không còn tự zoom/pan gây rung toàn khung.
- Có thể nhập prompt và dùng Veo 3.1 để tạo chuyển động tay/chân, người, khói, lửa hoặc ánh sáng từ ảnh nguồn. Cần Gemini API key và tài khoản có quyền dùng Veo.
- File sóng dạng PNG/GIF/MOV/MP4 chạy lặp độc lập, không co giãn theo âm lượng nhạc; có tùy chọn xóa nền trắng.
- Veo tạo video 24 fps nên ứng dụng tự chuyển FPS project về 24 sau khi tạo để giữ nhịp khung hình mượt.

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
