# Secure Release Package

## Deskripsi

Project ini adalah paket release aman untuk menjalankan aplikasi dashboard monitoring dan server lokal secara cepat di lingkungan Windows. Tujuan utamanya adalah membuat aplikasi dapat dijalankan tanpa menaruh data kredensial sensitif secara langsung di file source code.

Aplikasi ini mencakup:
- server API lokal untuk menerima heartbeat dan perintah client
- dashboard Streamlit untuk monitoring perangkat dan jaringan
- client agent yang mengirimkan heartbeat ke server
- konfigurasi runtime yang diambil dari environment variables

## Fitur utama

- dashboard berbasis Streamlit
- API server lokal di port 5000
- dashboard UI di port 8501
- konfigurasi sensitif dipisahkan dari source code
- script otomatis untuk install dependencies dan menjalankan aplikasi

## Struktur folder

```text
secure_release/
├─ .env.example
├─ .gitignore
├─ README.md
├─ install_and_run.bat
├─ requirements.txt
├─ dashboard.py
├─ server.py
├─ client.py
├─ clients_data.json
├─ client_aliases.json
├─ client_commands.json
├─ start_all.bat
├─ build_client_exe.bat
└─ ...
```

## Persyaratan sistem

Sebelum menjalankan aplikasi, pastikan sudah tersedia:

- Python 3.10 atau lebih tinggi
- pip
- koneksi ke jaringan lokal yang sama dengan router / client
- akses ke router lokal jika ingin mengambil data DHCP / perangkat jaringan

## Langkah instalasi

### 1. Salin template environment

Buat file `.env` dari `.env.example`:

```bat
copy .env.example .env
```

### 2. Ubah nilai yang wajib disesuaikan

Buka file `.env` dan ubah nilai berikut sesuai lingkungan Anda:

```env
API_BASE_URL=http://127.0.0.1:5000
CLIENT_SECRET_TOKEN=REPLACE_WITH_STRONG_TOKEN
DEFAULT_GATEWAY=10.10.10.1
ROUTER_IP=10.10.10.1
ROUTER_USERNAME=admin
ROUTER_PASSWORD=REPLACE_WITH_ROUTER_PASSWORD
STREAMLIT_PORT=8501
API_PORT=5000
```

Nilai yang harus diubah pengguna sendiri:

- `CLIENT_SECRET_TOKEN`
  - gunakan token rahasia yang unik dan sulit ditebak
  - jangan gunakan nilai default seperti `REPLACE_WITH_STRONG_TOKEN`
- `ROUTER_USERNAME`
  - isi username login router Anda
- `ROUTER_PASSWORD`
  - isi password login router Anda
- `ROUTER_IP`
  - sesuaikan dengan IP router lokal Anda
- `DEFAULT_GATEWAY`
  - sesuaikan dengan gateway jaringan Anda
- `API_BASE_URL`
  - sesuaikan jika server API berjalan di IP lain, bukan localhost

### 3. Install dependencies

Jalankan:

```bat
install_and_run.bat
```

Script ini akan:
- memeriksa Python
- menginstal dependency dari `requirements.txt`
- cek apakah `.env` sudah dibuat
- menghentikan proses lama yang memakai port 5000 atau 8501
- menjalankan server API
- menjalankan dashboard Streamlit

## Menjalankan aplikasi secara manual

Jika ingin menjalankan tanpa batch file, gunakan perintah berikut:

```bat
py -m pip install -r requirements.txt
py server.py
py -m streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

## Detail konfigurasi penting

### 1. Secret token

File utama tidak boleh menyimpan token asli. Token harus disimpan di file `.env` atau di environment variable sistem.

Contoh:

```env
CLIENT_SECRET_TOKEN=token_anda_yang_sangat_rahasia
```

Jangan pernah commit file `.env` ke GitHub jika berisi data asli.

### 2. Router credential

Router sering memerlukan username dan password login. Nilai ini harus disesuaikan dengan hardware router / access point yang digunakan.

Obligasi pengguna:
- ubah `ROUTER_USERNAME`
- ubah `ROUTER_PASSWORD`
- periksa kesesuaian IP pada `ROUTER_IP`

### 3. API endpoint

Jika server berjalan di server lain, sesuaikan `API_BASE_URL` misalnya:

```env
API_BASE_URL=http://10.10.10.10:5000
```

### 4. Port yang dipakai

Default yang digunakan:

```env
API_PORT=5000
STREAMLIT_PORT=8501
```

Jika port tersebut sudah dipakai oleh aplikasi lain, ubah nilainya dan sesuaikan launcher yang dipakai.

## Pengaturan aman yang disarankan

Sebelum upload ke GitHub, pastikan hal berikut:

- file `.env` tidak ikut di-commit
- file data yang berisi secret tidak dipublikasikan
- token dibuat unik untuk setiap lingkungan
- password router bukan versi default atau weak password
- gunakan `.gitignore` untuk mengecualikan file sensitif

Contoh `.gitignore` yang disarankan:

```gitignore
.env
*.log
__pycache__/
*.pyc
```

## Troubleshooting

### Dashboard tidak muncul

Cek:
- apakah `streamlit` sudah terinstall
- apakah port 8501 masih dipakai
- apakah server API sudah berjalan
- apakah file `.env` sudah dibuat dengan nilai yang benar

### API tidak merespons

Cek:
- port 5000 tidak diblokir
- `CLIENT_SECRET_TOKEN` sama di client dan server
- server masih berjalan

### Router tidak bisa login

Cek:
- username login router benar
- password benar
- IP router benar
- model router sesuai dengan parameter yang digunakan pada aplikasi

## Catatan penting

Project ini dibuat untuk lingkungan internal dan penggunaan lokal. Saat distribusi ke repository publik, pastikan semua data sensitif telah disesuaikan dan tidak disertakan dalam commit.

## Lisensi

Silakan sesuaikan lisensi sesuai kebutuhan organisasi atau proyek Anda.

## Penutup

Dengan mengikuti langkah di atas, pengguna cukup mengubah parameter yang diperlukan di file `.env`, lalu menjalankan `install_and_run.bat` untuk memulai aplikasi. Pastikan semua nilai kredensial dan alamat jaringan sudah disesuaikan sebelum diunggah ke repositori publik.
