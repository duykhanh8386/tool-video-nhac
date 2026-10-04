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

- Có màn hình **AI Video hàng loạt** dùng chung cho Seedance qua BytePlus LAS API chính thức, Veo qua Gemini API và các model ComfyUI local. Màn hình nhận prompt đơn/TXT/CSV, ảnh lẻ/thư mục, lọc và khôi phục job, retry có giới hạn, xuất báo cáo CSV/JSON và kiểm tra MP4 bằng FFprobe.
- API key Gemini/BytePlus được lưu trong Windows Credential Manager. Hệ thống không nhập cookie/session, không xoay proxy hay đổi tài khoản để né quota. Có thể đặt ngân sách ngày/batch cùng đơn giá cloud ước tính để chặn batch trước khi gửi.
- Ảnh nền mặc định dùng camera cố định, không còn tự zoom/pan gây rung toàn khung.
- Có bốn chế độ AI local qua ComfyUI: **Wan Chất lượng 20 bước**, **Wan DMD 4 bước**, **LTX-Video 2B Distilled 8 bước** và **HunyuanVideo 1.5 480p 8 bước**. Tất cả render bằng máy hiện tại, không dùng token/API key/credit.
- Ngay trong phần **Nguồn ảnh AI**, có thể chọn một ảnh, chọn nhiều ảnh riêng lẻ hoặc chọn cả thư mục (kể cả thư mục con). Một prompt dùng chung sẽ tạo clip AI cho từng ảnh theo hàng đợi, đưa từng clip vào cùng bố cục, rồi xuất đủ số video.
- Có thể nhập prompt và dùng Veo 3.1 để tạo chuyển động tay/chân, người, khói, lửa hoặc ánh sáng từ ảnh nguồn. Cần Gemini API key và tài khoản có quyền dùng Veo.
- Có chế độ **Google Vids Web — Beta** để dùng quota Google AI của tài khoản trên web thay cho GPU/API key: đăng nhập Google một lần trong profile Chrome riêng, chọn một hoặc nhiều ảnh và prompt trong app, tool lần lượt tạo/tải MP4 rồi đưa clip vào đúng bố cục để render visual 60 giây. Tool không lưu mật khẩu Google.
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
- Nút **Render toàn bộ background đã chọn** dùng lại trực tiếp danh sách ảnh ở **Nguồn ảnh AI**; sau khi tạo AI hàng loạt, nút tự dùng các clip AI đã lưu trong state/project nên không phải chọn thư mục lần hai.
- Batch AI chạy hai pha: tạo và lưu toàn bộ clip trước, sau đó dùng một snapshot cố định của preview để render từng visual 60 giây với đầy đủ text, logo, ảnh, sóng, hiệu ứng và font.

## Google / YouTube OAuth và YouTube Studio

Màn hình **Tài khoản YouTube** dùng luồng OAuth 2.0 dành cho ứng dụng desktop, Authorization Code + PKCE:

1. Trong Google Cloud Console, bật **YouTube Data API v3**, cấu hình OAuth consent screen và tạo OAuth client loại **Desktop app**.
2. Tải client JSON về máy, mở **Tài khoản YouTube** và chọn file đó. Nếu ứng dụng còn ở chế độ Testing, thêm tài khoản được phép vào danh sách test users.
3. Nhập email làm `login_hint`/nhãn nhận diện và nên nhập Channel ID cần dùng, rồi bấm **Đăng nhập Google**. Trình duyệt hệ thống sẽ nhận xác nhận; callback chỉ lắng nghe trên `127.0.0.1`.
4. Ứng dụng chỉ xin scope `youtube.readonly`, gọi `channels.list(mine=true)` để đối chiếu Channel ID, và lưu refresh token theo kênh trong Windows Credential Manager. Access token chỉ được giữ trong bộ nhớ và tự làm mới khi hết hạn.
5. Dùng **Đăng nhập lại** khi Google thu hồi phiên. **Ngắt kết nối + thu hồi token** gọi endpoint thu hồi của Google rồi xóa credential cục bộ.

Email không được tự điền vào trang đăng nhập và ứng dụng không có trường mật khẩu Google. Token, cookie và Authorization header không được ghi vào log hoặc thông báo lỗi.

Nút **Mở YouTube Studio** dùng một `user-data-dir` Chrome/Edge riêng cho từng tài khoản. Lần đầu người dùng tự hoàn tất đăng nhập, CAPTCHA hoặc 2FA trong cửa sổ trình duyệt bình thường; các lần sau cùng profile được mở lại. Nếu phiên web hết hạn, Google tự hiển thị yêu cầu xác thực lại. Luồng này không dùng Selenium để nhập thông tin đăng nhập hoặc vượt bước bảo vệ.

## Google Vids Web — Beta

1. Cài Google Chrome hoặc Microsoft Edge trên máy, chọn engine **Google Vids Web — dùng quota Google AI Ultra (Beta)** rồi bấm **Đăng nhập Google Vids (chỉ lần đầu)**.
2. Tự đăng nhập trong cửa sổ trình duyệt riêng, mở được Google Vids và đóng cửa sổ. Phiên đăng nhập chỉ nằm trong profile `VisualLoopStudio/GoogleVidsChromeProfile` của máy hiện tại; mỗi máy đăng nhập riêng.
3. Chọn một ảnh hoặc danh sách/thư mục ảnh ở **Nguồn ảnh AI**, nhập prompt chung. Ảnh đơn dùng nút tạo clip; nhiều ảnh dùng nút tạo Google Vids + render toàn bộ.
4. MP4 trung gian được tải vào `Google_Vids_Clips`. Với hàng loạt, tool chờ tạo đủ các clip có thể tạo rồi tự lặp từng clip trong visual 60 giây cùng toàn bộ text/logo/sóng/overlay/bố cục đã lưu.

Google chưa cung cấp API công khai để tạo clip Google Vids, nên chế độ này điều khiển giao diện web và được đánh dấu Beta. Giao diện Google, CAPTCHA, chính sách tài khoản hoặc quota có thể làm tác vụ dừng; khi đó bật **Hiện Chrome khi chạy Vids** để xem bước cần xử lý. Bản EXE đóng gói bộ điều khiển Playwright nhưng dùng Chrome/Edge đã cài trên máy, không nhúng hoặc lưu mật khẩu trình duyệt.

## Muse AI — batch ảnh thành video trên ba tài khoản

Trang **Muse Batch — 3 tài khoản** quét JPG/JPEG/PNG/WEBP trong một thư mục, chia ảnh round-robin cho ba Chrome profile cố định tại `data/muse_profiles/account_1`, `account_2`, `account_3`, rồi chạy ba worker tạo video đồng thời với cùng prompt và cài đặt.

- Mỗi phiên có Selenium driver, worker thread, task, lock và Chrome `user-data-dir` riêng; không chia sẻ cookie, window handle hoặc trạng thái đăng nhập.
- Tool chỉ tương tác trên `muse.ai`, `auth.muse.ai` và `accounts.google.com`, đồng thời chỉ tự chọn đúng email đã cấu hình khi Google đã hiển thị sẵn tài khoản đó.
- Mật khẩu, CAPTCHA, 2FA, xác minh thiết bị và quyền mới luôn do người dùng hoàn tất trong Chrome. Phiên chuyển sang `LOGIN_REQUIRED`, giữ Chrome mở và tự tiếp tục sau callback hợp lệ.
- Mỗi ảnh là một job có ID theo đường dẫn/metadata/hash ảnh/hash prompt/cài đặt. Checkpoint giữ trạng thái sau từng bước; job đã submit chỉ được khôi phục kết quả/download, không tự bấm Generate lần hai.
- Video mới được phân biệt với kết quả cũ, tải thành MP4 theo tên `<ảnh>__<account_id>__<job_id>.mp4` và kiểm tra file hoàn tất trước khi đánh dấu thành công. Rate limit/quota chỉ dừng đúng worker đó và không chuyển ảnh sang tài khoản khác.
- Checkpoint chỉ chứa email nhãn, đường dẫn, prompt, cài đặt, mapping output và trạng thái; không chứa mật khẩu, cookie hoặc token. Dựng lại UI sẽ nối với manager còn chạy thay vì tạo driver trùng.
- **Dừng tất cả Muse** chỉ đặt cờ dừng cho ba tác vụ Muse, không gọi hủy Auto Registry hay YouTube.

Quy trình sử dụng: đăng nhập đủ ba tab đến trạng thái `READY`, chọn thư mục ảnh và output, nhập prompt chung, bấm **Phân bổ ảnh** để xem trước, rồi **Bắt đầu cả 3**. Có thể dừng/tiếp tục, chạy lại ảnh lỗi hoặc phân bổ lại riêng các ảnh chưa submit.

Provider **Muse AI Web** trong **AI Video hàng loạt** vẫn dùng tài khoản được chọn và không tự xoay tài khoản để né quota.

Mỗi lần có commit mới được push lên nhánh `Dola-AI`, GitHub Actions sẽ:

1. Chạy toàn bộ kiểm thử.
2. Đóng gói EXE portable kèm FFmpeg/FFprobe.
3. Tạo checksum SHA-256.
4. Tạo GitHub Release mới và đánh dấu là bản mới nhất.

Nút **Kiểm tra cập nhật** chỉ đọc các release có tag `dola-ai-v*` do workflow của nhánh `Dola-AI` tạo và so sánh commit đã đóng dấu trong EXE. Release từ `main` hoặc nhánh khác sẽ bị bỏ qua.

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
