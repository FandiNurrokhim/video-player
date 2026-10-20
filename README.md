# Pemutar Video

Pemutar video desktop untuk Windows yang dibuat dengan **Python + PySide6 (Qt 6)**. Video dibaca lewat FFmpeg yang sudah termasuk di Qt, jadi bisa memutar MKV, MP4, AVI, WebM, dan format lain. Cocok untuk menonton anime per folder: ada pustaka folder dengan thumbnail, tombol lewati intro, dan video terakhir otomatis dilanjutkan.

## Fitur

- **Pustaka folder di sidebar kanan** (gaya YouTube): thumbnail, judul, dan durasi tiap video. Folder bisa dibuka/ditutup, kartu folder menempel di atas saat di-scroll, dan ada pencarian berdasarkan nama video atau folder.
- **Lanjutkan otomatis**: saat aplikasi dibuka, video terakhir terbuka lagi di menit terakhir.
- **Next / Prev** episode di folder yang sama, dan otomatis lanjut ke episode berikutnya.
- **Lewati Intro**: lompat dari 01:30 ke 02:30, dengan tombol yang bisa ditutup.
- **Efek ambient**: cahaya di sekitar video dan sidebar mengikuti warna video. Blur, sebaran, kecerahan, dan kecepatannya bisa diatur.
- **Picture-in-Picture**: jendela kecil yang selalu di atas.
- Kontrol mengambang ala Films & TV, subtitle & pilihan trek audio, layar penuh, seret-lepas file/folder.

## Kebutuhan

- Windows 10/11
- Python **3.10+** (dikembangkan dengan Python 3.14)

## Menjalankan dari source

```powershell
git clone https://github.com/FandiNurrokhim/video-player.git
cd video-player

python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

python player.py
```

Bisa juga langsung membuka file atau folder tertentu:

```powershell
python player.py "D:\Anime\Episode 01.mkv"
python player.py "D:\Anime"
```

## Build menjadi .exe

> Tutup dulu aplikasi Pemutar Video sebelum build. Kalau `dist\player\player.exe` masih berjalan, build gagal dengan `PermissionError: [WinError 5] Access is denied`.

```powershell
venv\Scripts\activate
pyinstaller --noconfirm --clean player.spec
```

Hasil build ada di **`dist\player\player.exe`**. Folder `build\` hanya berisi file sementara, jadi jangan jalankan exe dari sana.

Untuk memindahkan atau membagikan aplikasi, salin **seluruh folder `dist\player`**, karena `player.exe` membutuhkan folder `_internal` di sebelahnya.

## Pintasan keyboard

| Tombol | Fungsi |
| --- | --- |
| `Spasi` | Play / pause |
| `←` / `→` | Mundur / maju 5 detik |
| `↑` / `↓` | Volume naik / turun |
| `M` | Bisu |
| `Ctrl+→` / `PageDown` | Video berikutnya |
| `Ctrl+←` / `PageUp` | Video sebelumnya |
| `S` | Lewati intro (saat tombolnya tampil) |
| `A` | Efek ambient on/off |
| `P` | Picture-in-Picture |
| `F` / klik ganda | Layar penuh |
| `Esc` | Keluar layar penuh / PiP |
| `Ctrl+L` | Tampil / sembunyikan pustaka |
| `Ctrl+O` | Buka file |
| `Ctrl+Shift+O` | Tambah folder ke pustaka |

## Pengaturan

Konstanta di bagian atas `player.py` bisa diubah sesuai selera:

| Konstanta | Bawaan | Keterangan |
| --- | --- | --- |
| `INTRO_START` / `INTRO_END` | `90_000` / `150_000` | Rentang tombol Lewati Intro (milidetik) |
| `HIDE_AFTER_MS` | `2500` | Kontrol disembunyikan setelah mouse diam |
| `AMBIENT_DEFAULTS` | lihat kode | Nilai awal efek ambient (bisa diubah juga lewat tombol ⚙ di aplikasi) |

Daftar folder, volume, posisi terakhir, dan pengaturan ambient disimpan otomatis di registry Windows (`HKCU\Software\PemutarVideo`). Cache thumbnail ada di `%LOCALAPPDATA%\Pemutar Video\cache\thumbs`.

## Struktur proyek

```
player.py              # seluruh kode aplikasi
player.spec            # konfigurasi build PyInstaller
requirements.txt
icon.ico               # ikon aplikasi
icon_play.png          # gambar pegangan progress bar
icon_play_sidebar.png  # penanda video yang sedang diputar di sidebar
```
