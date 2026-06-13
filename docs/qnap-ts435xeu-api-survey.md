# QNAP TS-435XeU API / Custom CSI Feasibility Survey

Date: 2026-06-13

## 結論

非公式に QNAP CSI 相当を自作して、少なくとも「RWX SMB を非 root の CDI/KubeVirt から書けるようにする」ことは技術的に可能です。Node 側で `mount.cifs` を直接制御し、`uid=107,gid=107,forceuid,forcegid,file_mode=0660,dir_mode=0770,noperm` のような mount option を確実に渡せば、公式 QNAP CSI の現在の詰まりどころは避けられます。

ただし「golden image を高速にコピーして VM を作成する」まで満たすには、QNAP 側で共有フォルダまたはファイルをサーバーサイド clone/snapshot できる API/CLI が必要です。公開情報だけでは、Storage & Snapshots の共有フォルダ作成、snapshot、clone を安定して外部操作できる公式 REST API は確認できませんでした。ここは TS-435XeU 実機で、QTS の非公開 Web API か SSH/CLI を調査する必要があります。

最も現実的な開発方針は 2 段階です。

1. 自作 SMB CSI の最小版で、既存共有または CSI 専用共有配下のディレクトリを RWX PV として作り、正しい CIFS mount option で CDI/KubeVirt の書き込みを通す。
2. 実機で QNAP の Storage & Snapshots 操作 API/CLI を特定し、PVC clone / VolumeSnapshot restore をサーバーサイド clone に置き換える。

## 現状の問題

CDI importer は `/scratch/tmpimage` への転送までは完了しています。失敗しているのは raw 変換先 `/data/disk.img` の作成です。

```text
qemu-img: /data/disk.img: error while converting raw:
Could not create '/data/disk.img': Permission denied
```

実機確認では、QNAP CSI の SMB volume は CIFS mount が `uid=0,gid=0,file_mode=0755,dir_mode=0755` になっており、CDI importer の UID/GID 107 から書けません。StorageClass に `mountOptions` を追加しても PV には反映される一方、実際の CIFS mount には渡されませんでした。root pod ならファイル作成はできましたが、`chown` / `chmod` は有効に見えず、KubeVirt の非 root QEMU 実行にも同じ問題が残ります。

## QNAP CSI 公式リポジトリの確認結果

公式リポジトリは `qnap-dev/QNAP-CSI-PlugIn` です。公開されている内容は README、サンプル YAML、Helm chart、バイナリ中心で、CSI driver の実装ソースは見当たりませんでした。つまり、既存 driver を fork して mount option だけ直す、という進め方は現実的ではありません。実装するなら新規 CSI driver になります。

README 上の QNAP CSI v1.6.0 の前提は次の通りです。

| 項目 | 内容 |
| --- | --- |
| 対応 Kubernetes | v1.24 - v1.35 |
| 対応 QNAP OS | QTS 5.1.0 以降、QuTS hero h5.1.0 以降 |
| protocol | iSCSI, SMB |
| access mode | RWO, RWX |
| data services | snapshots, cloning, expansion |
| SMB StorageClass | `trident.qnap.io/fileProtocol: "smb"` と node-stage secret |
| PVC | iSCSI は RWO、Samba は RWX |
| PVC clone | `dataSource.kind: PersistentVolumeClaim` をサポートする記述あり |
| VolumeSnapshot | snapshot 作成と snapshot から PVC 作成の記述あり |

重要な制約として、GitHub Issue #16 で QNAP collaborator が SMB の `mountOptions` 非対応を明言しています。また、SMB StorageClass では uid/gid が root:root のままになる理解も正しいと回答されています。今回の実機ログはその挙動と一致しています。

## TS-435XeU の前提

QNAP の TS-435XeU 仕様ページでは、TS-435XeU-4G は Marvell OCTEON TX2 CN9130/CN9131 ARMv8 Cortex-A72 4-core 2.2GHz、64-bit ARM、QTS firmware 5.0.1 以降の記載があります。QNAP CSI README は ARM64/ARMv7 をサポート対象に含めているため、アーキテクチャ面では CSI node plugin の自作・実行は可能な範囲です。

ただし高速 clone の可否は CPU/OS ではなく、NAS 側のストレージプール種別、QTS/QuTS hero、共有フォルダ snapshot/clone 機能、CLI/API の有無に依存します。

## 見つかった API 面

| API 面 | パス / 入口 | 認証 | CSI への有用性 | 評価 |
| --- | --- | --- | --- | --- |
| QTS login CGI | `/cgi-bin/authLogin.cgi` | user/password から SID | 他 API の入口 | 使える可能性が高い |
| QTS sysinfo CGI | `/cgi-bin/management/manaRequest.cgi?subfunc=sysinfo` | SID | モデル/リソース確認 | 補助用途 |
| File Station API | `/cgi-bin/filemanager/utilRequest.cgi` | SID | 既存共有内のファイル/フォルダ操作 | PV provision には不足 |
| Container Station API | `/container-station/api/v1` | app login | CSI とは無関係 | 不採用 |
| Virtualization Station API | `/qvs` | QTS login + QVS login + CSRF | QVS VM clone/snapshot | Kubernetes PV には直接使えない |
| QNAP CSI / Trident CRD | Kubernetes CRD | Kubernetes auth + backend secret | 公式 driver 経由の provision/clone/snapshot | SMB mount 権限で詰まる |
| Storage & Snapshots Web API | QTS UI 裏の CGI/REST と推定 | QTS session | 共有フォルダ、snapshot、clone の本命 | 実機 capture が必要 |
| SSH / QNAP CLI | NAS SSH | NAS user/key/password | 非公開 API の代替 | 実機 discovery が必要 |

File Station API は、既存共有配下にディレクトリやファイルを作る用途には使えそうです。ただし StorageClass から PV を動的作成するには、容量管理、共有フォルダ作成、削除、snapshot、clone が必要です。File Station だけでは CSI backend として弱いです。

Virtualization Station API は VM clone や snapshot のエンドポイントが見つかりますが、これは QNAP Virtualization Station 内の VM 操作用です。KubeVirt PVC の高速 clone にそのまま使う API ではありません。ただし QNAP アプリが `/qvs` のような REST API を持ち、QTS session と CSRF で操作されることは参考になります。Storage & Snapshots も UI 裏に同種の非公開 API がある可能性は高いです。

## 要件への適合評価

### RWX filesystem volume

自作 CSI で達成可能です。CSI Node service で SMB mount を担当し、公式 QNAP CSI が無視している mount option を確実に渡します。

想定 mount option:

```text
vers=3.1.1,uid=107,gid=107,forceuid,forcegid,file_mode=0660,dir_mode=0770,noperm,cache=strict,actimeo=1
```

CDI/KubeVirt の UID/GID に固定するか、StorageClass/PVC annotation から uid/gid を指定できるようにします。KubeVirt 用に絞るなら固定値のほうが初期実装は単純です。

### CDI import

自作 CSI で `/data/disk.img` 作成は通せる見込みです。CDI importer を root にする案より筋が良いです。root 化は import の一部を通すだけで、VM 起動時に QEMU が非 root で disk image を更新する問題を残します。

### KubeVirt runtime

要検証です。CIFS 上の raw image を QEMU が安定して扱えるか、lock/cache/flush/rename/fsync の挙動を確認する必要があります。最低限、次を検証します。

1. CDI importer UID 107 で `qemu-img convert` が完了すること。
2. KubeVirt launcher 側の QEMU が同じ disk image を作成・更新できること。
3. live migration または VM 再スケジュール時に RWX volume として再 mount できること。
4. abrupt shutdown 後の image consistency を確認すること。

### golden image 高速 clone

公開情報だけでは「達成可能」と断言できません。公式 QNAP CSI は PVC clone / VolumeSnapshot をサポートすると書いていますが、driver 実装が非公開です。自作 driver で同等の高速 clone を実装するには、NAS 側に以下のどれかが必要です。

| 方法 | 成立条件 | 期待速度 | リスク |
| --- | --- | --- | --- |
| 共有フォルダ snapshot clone | Storage & Snapshots API/CLI で share snapshot から clone できる | 速い | 非公開 API 依存 |
| QuTS hero FastClone / reflink | 同一 pool 上のファイル clone が API/CLI で可能 | 速い | TS-435XeU が QTS の場合は不可の可能性 |
| QNAP CSI 相当の内部 clone API 再利用 | QNAP CSI が使う backend API を特定できる | 速い | driver source がない |
| FileStation copy | FileStation API でファイルコピー | 遅い可能性 | full copy になりがち |
| Kubernetes 側 rsync/qemu-img copy | 普通のデータコピー | 遅い | 要件とずれる |

現時点の判断は「RWX 書き込みは自作 CSI で可能、サーバーサイド高速 clone は実機 API/CLI 調査が必要」です。

## 自作 CSI の実装案

### Phase 1: SMB RWX driver

目的は公式 QNAP CSI の SMB mount 権限問題を回避し、CDI/KubeVirt で使える RWX Filesystem volume を提供することです。

構成:

| コンポーネント | 内容 |
| --- | --- |
| ControllerCreateVolume | 既存 base share 配下に volume directory を作成 |
| ControllerDeleteVolume | volume directory を削除または retain |
| NodeStageVolume | `mount.cifs` で global mount |
| NodePublishVolume | bind mount |
| NodeUnpublish/Unstage | unmount |
| ControllerExpandVolume | quota/API が見つかるまで no-op または unsupported |
| CreateSnapshot | Phase 1 では unsupported |
| CreateVolume from source | Phase 1 では full copy か unsupported |

Backend は最初から Storage & Snapshots へ踏み込まず、CSI 専用の既存共有を 1 つ作って、その中に PV ごとのディレクトリを切る方式が低リスクです。共有フォルダ単位の容量制限は弱くなりますが、まず KubeVirt が動くかを短期間で検証できます。

### Phase 2: QNAP provision / clone

目的は StorageClass から共有フォルダを作成し、golden image を高速 clone できるようにすることです。

候補:

| backend 操作 | 望ましい実装 |
| --- | --- |
| CreateVolume | QNAP の共有フォルダ作成 API/CLI |
| DeleteVolume | 共有フォルダ削除 API/CLI |
| CreateSnapshot | 共有フォルダ snapshot |
| DeleteSnapshot | snapshot 削除 |
| CreateVolume from Snapshot | snapshot clone |
| CreateVolume from PVC | snapshot + clone、または file/block clone |

この段階は QNAP の非公開仕様に依存します。API が firmware update で変わる可能性があるため、driver 側にバージョン検出、dry-run、明確なエラー、保守用 integration test が必要です。

## 実機で必要な調査

秘密情報を記録しない前提で、次の順に確認します。

1. QTS/QuTS hero バージョン、storage pool 種別、共有フォルダ snapshot/clone 機能の有無を確認する。
2. Browser DevTools で Storage & Snapshots UI の「共有フォルダ作成」「snapshot」「snapshot clone」「削除」操作時の HTTP request を capture する。
3. SSH で QNAP CLI を列挙する。

```sh
find /sbin /usr/sbin /bin /usr/bin -maxdepth 1 -type f \
  | grep -Ei 'qcli|storage|snapshot|share|folder|lun|iscsi|smb'
```

4. 候補 CLI の help と read-only query を確認する。

```sh
qcli_storage -h
qcli_sharedfolder -h
storage_util --help
```

5. disposable な test share で create/delete/snapshot/clone を試す。
6. clone された share を Linux node から `mount.cifs` し、uid/gid option が期待通り効くことを確認する。
7. その mount 上で `qemu-img convert`, `qemu-img info`, KubeVirt VM 起動を通す。

## リスク

| リスク | 内容 | 対策 |
| --- | --- | --- |
| 非公開 API 変更 | QTS firmware update で壊れる | バージョン検出、互換 test、明確な support 範囲 |
| データ破壊 | 削除/clone API の誤用 | CSI volume 名と QNAP object 名の厳格な対応、retain policy、dry-run |
| セキュリティ | NAS admin credential を CSI controller に持たせる | 最小権限ユーザー、Secret 分離、ログに credential を出さない |
| CIFS 上の VM image | QEMU と SMB の整合性/性能 | KubeVirt 実 workload で fsync/停止/再起動 test |
| 容量管理 | 既存共有配下ディレクトリ方式では PV quota が弱い | Phase 2 で共有フォルダ単位 provision または quota API を実装 |
| 保守性 | QNAP 公式 source がない | 小さく作り、QNAP 固有 API 層を隔離 |

## 実装可否の判断

| 要件 | 可否 | 根拠 |
| --- | --- | --- |
| RWX を使いたい | 可能 | SMB/CIFS driver として自作すれば mount option を制御できる |
| iSCSI は使わない | 可能 | SMB RWX driver として作る |
| CDI import を通したい | 可能性高い | 現在の失敗は `/data` の権限で、mount option で解決可能 |
| VM を実行したい | 可能性あり、要検証 | QEMU on CIFS の挙動確認が必要 |
| golden image から高速作成 | 未確定 | QNAP 側の server-side clone API/CLI が必要 |
| 公式 QNAP CSI を patch したい | 困難 | source が公開されていない |
| 公式 QNAP CSI の SMB mountOptions で直す | 不可 | QNAP collaborator が非対応と回答 |

## 推奨

要件を達成するための現実的な次アクションは、自作 CSI の前に「NAS 側 clone API/CLI の実機確認」を 1 日程度で先に行うことです。ここで共有フォルダ snapshot clone または file clone の入口が見つかれば、自作 CSI は要件に届きます。見つからない場合でも Phase 1 の SMB RWX driver は作れますが、golden image は full copy になり、速度面では NFS CSI や Longhorn と大きく変わらない可能性があります。

したがって、開発可否は次の判定にします。

```text
RWX + CDI/KubeVirt 書き込み: 自作で達成可能
RWX + golden image 高速 clone: QNAP 実機 API/CLI 調査で clone primitive が見つかれば達成可能
```

## 参照

- QNAP TS-435XeU hardware specs: https://www.qnap.com/en/product/ts-435xeu/specs/hardware
- QNAP CSI PlugIn repository: https://github.com/qnap-dev/QNAP-CSI-PlugIn
- QNAP CSI README: https://github.com/qnap-dev/QNAP-CSI-PlugIn/blob/main/readme.md
- QNAP CSI Issue #16: https://github.com/qnap-dev/QNAP-CSI-PlugIn/issues/16
- SMB `mountOptions` 非対応の回答: https://github.com/qnap-dev/QNAP-CSI-PlugIn/issues/16#issuecomment-3484166402
- SMB root:root 挙動の回答: https://github.com/qnap-dev/QNAP-CSI-PlugIn/issues/16#issuecomment-3594375116
- QNAP File Station / Container Station API sample: https://github.com/g1zm0e/QNAP-ContainerStation-and-Filestation-API/blob/main/qnap.py
- QNAP File Station Go client: https://github.com/nine-lives-later/go-qnap-filestation
- QNAP Virtualization Station client sample: https://github.com/arnstarn/mcp-server-qnap-qvs/blob/main/src/mcp_server_qnap_qvs/qvs_client.py
- QNAP Virtualization Station Go SDK: https://github.com/tmeckel/qnap-qvs-sdk-for-go
