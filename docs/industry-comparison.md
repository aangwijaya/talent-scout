# Workflow kita dibanding pola industri, dan jalan menuju level enterprise

> Ditulis 2026-11-23, berdasarkan pengetahuan sampai pertengahan 2026. Nama produk dan fiturnya cepat berubah, jadi periksa ulang sebelum dipakai untuk keputusan.
> Konteks: `BLUEPRINT.md` (arsitektur), `WORKFLOW.md` (cara pakai), dan `docs/adr/` (alasan keputusan).

## Jawaban singkat

Sebagian besar komponen workflow ini sudah lazim di industri, tapi cara kita merangkainya tidak umum. Beberapa bagiannya jarang ditemui, dan justru bagian itulah nilai jualnya.

## Pola yang umum dipakai perusahaan

| Pola | Contoh | Kemiripan dengan kita |
|---|---|---|
| **Satu agent di IDE atau CLI, manusia mengawasi langsung** | Copilot, Cursor, Claude Code lokal | Paling banyak dipakai. Tidak ada orchestrator, dan sandbox-nya biasanya hanya izin per perintah. |
| **Agent "background" di cloud:** issue masuk, agent jalan di sandbox, lalu membuka PR | Copilot coding agent, Codex cloud, Devin, Jules, Claude Code di web | **Paling mirip.** Tiap tugas punya sandbox VM, ada allowlist jaringan, keluarannya PR, dan manusia yang review. Bedanya, orkestrasinya milik platform vendor dan biasanya hanya satu agent. |
| **Framework multi-agent** | LangGraph (pola supervisor), CrewAI, AutoGen, Agents SDK dengan handoff | Multi-agent, tapi orchestrator-nya LLM yang membagi tugas. Kita sengaja tidak memakai ini (ADR 0001). |
| **Workflow engine deterministik dengan langkah AI** | Temporal, Airflow, pipeline CI dengan langkah LLM | Filosofinya mirip orchestrator kita, yaitu alurnya berupa kode. Tapi jarang dipakai khusus untuk agent coding. |
| **Spec-driven development** | spec → plan → tasks → agent, dengan `AGENTS.md`/`CLAUDE.md` | Mirip langkah ide → grilling → ADR → node di workflow kita. |

## Yang di workflow kita sudah standar industri

- Issue menjadi PR, CI sebagai gate, branch protection, dan manusia yang merge.
- Sandbox per tugas, dan allowlist egress.
- File brief untuk agent (`CLAUDE.md`/`AGENTS.md`), dan satu worktree per tugas.

## Yang jarang ditemui (pembeda kita)

1. **Orchestrator berupa kode yang menjalankan graph dependensi issue** (node graph). Kebanyakan sistem memakai antrean tugas sederhana (satu issue, satu agent), atau LLM sebagai pengatur. Graph node dengan fence dan `blocked_by` yang dihitung dari GitHub itu tidak umum.
2. **Gate "red proof":** test baru harus terbukti gagal di commit dasar. Ide TDD-nya lazim, tapi penegakan otomatis per PR untuk kode buatan agent jarang.
3. **Review silang antar model:** Claude me-review OpenCode dan sebaliknya. Kebanyakan perusahaan memakai satu vendor, atau satu bot review.
4. **Verifier yang menyabotase test.** Ini mutation testing, yang konsepnya sudah lama ada, tapi dijalankan sebagai langkah verifikasi per PR oleh agent. Ini jarang, dan buktinya kuat: di client#91, cara ini menemukan enam perilaku yang tidak tercakup test.
5. **Pelajaran berulang otomatis menjadi node berisi check atau lint**, bukan menumpuk di dokumen.
6. **Kontrak yang dikunci dengan hash antar repo**, dengan PR sync otomatis.
7. **Sandbox yang dipilot dan diuji pada dirinya sendiri** sebelum dipercaya, dengan probe dinding di setiap sesi.

## Di mana perusahaan besar berbeda (dan workflow kita lebih sederhana)

| Aspek | Perusahaan besar | Workflow kita sekarang |
|---|---|---|
| Isolasi | microVM (Firecracker), gVisor, atau sandbox terkelola (misalnya E2B) | Container Podman rootless di satu mesin (lihat batasan di BLUEPRINT §9.1) |
| Skala | Puluhan agent paralel di infrastruktur bersama | Satu mesin, satu runner per repo |
| Tata kelola | Broker secret, SSO, audit log terpusat, policy-as-code, anggaran biaya per tim | Log per node, spend cap, file env di mesin |
| Build vs beli | Kebanyakan membeli platform vendor | Control plane custom: transparan dan mudah dijelaskan, tapi harus dirawat sendiri |
| Review | Dibagi banyak orang (CODEOWNERS) | Semua merge lewat satu orang |
| Tempat orchestrator berjalan | CI atau server, 24/7 | Laptop atau WSL, lewat tmux |

## Posisi yang jujur untuk portofolio

> Bukan platform enterprise, tapi menerapkan prinsip yang sama (least privilege, egress allowlist, human-in-the-loop), ditambah beberapa lapisan verifikasi yang jarang dipakai: orchestrator deterministik, red proof, review silang, sabotase test, dan learning loop.

## Jalan menuju level enterprise (urutan yang saya sarankan)

| # | Langkah | Kenapa | Kira-kira apa yang berubah |
|---|---|---|---|
| 1 | **Sandbox microVM atau gVisor** | Isolasi kernel, sehingga exploit container tidak lagi berarti akses host | `container.py` diganti adapter ke runtime microVM (Firecracker, Kata, atau gVisor `runsc`) atau sandbox terkelola. Probe dinding tetap menjadi penentu. |
| 2 | **Orchestrator di CI atau server, bukan di laptop** | Jalan 24/7, tidak bergantung pada satu mesin, dan log terpusat | `ops/run` dijalankan sebagai service atau job terjadwal. State (`ops/.state`) pindah ke penyimpanan bersama. Kill switch menjadi flag di server. |
| 3 | **Audit log terpusat** | Siapa menjalankan apa, kapan, dengan biaya berapa, dan koneksi keluar mana saja | Log run, log egress, keputusan gate, dan merge dikirim ke satu sink (misalnya SIEM atau log store) dengan retensi. |
| 4 | **Broker secret** | Tidak ada lagi file token di disk | Token agent dan model diambil dari vault dengan masa berlaku pendek, per run. Token GitHub memakai GitHub App dengan izin minimal, bukan PAT. |
| 5 | **Auto-merge tier 1** (setelah target pilot terpenuhi) | Bottleneck merge manusia berkurang | `automerge: [1]` di `ops/tiers.yaml`, lalu tier 2 setelah datanya mendukung |
| 6 | **Review bertingkat dan CODEOWNERS** | Review tidak lagi bertumpu pada satu orang | Tier 3 wajib disetujui pemilik area, dan tier 2 cukup satu reviewer manusia setelah review agent dan verifier |
| 7 | **Paralelisme dan kuota** | Banyak node berjalan bersamaan tanpa kehabisan kuota | Antrean dengan batas konkurensi per repo, akun atau API key terpisah untuk agent dan untuk lead agent, dan anggaran harian |
| 8 | **Policy-as-code** | Aturan bisa diaudit dan dites | Tier, fence, allowlist egress, dan batas sumber daya ditulis sebagai policy (misalnya OPA/Rego) dan dicek di CI |
| 9 | **Eval berkelanjutan** | Mengukur mutu agent dari waktu ke waktu | Metrik per node: persentase lolos gate di percobaan pertama, temuan High yang lolos, gap yang ketahuan di Scope, dan biaya per node |

## Yang tetap dipertahankan saat naik level

- Orchestrator tetap kode, bukan LLM (ADR 0001).
- Red proof, review silang, dan sabotase test oleh verifier.
- Manusia tetap memegang keputusan yang tidak bisa dibalik: kontrak, auth, migrasi, deploy, dan aturan agent (tier 3).
- Pelajaran menjadi check, bukan teks.
