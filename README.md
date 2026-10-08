# qbit-rar-cleaner

A qbit-rar-cleaner egy Debian/Ubuntu szerverre telepíthető program, amely két
egymástól külön indítható feladatot végez:

| Parancs | Feladat |
|---|---|
| qbit-rar-cleaner | A storage mappában talált RAR-készletek helybeni kibontása |
| qbit-iso-cleaner | A storage mappában talált ISO-k külön mountolása és kimásolása |

Mindkét mód a szerver valódi fájlrendszerét vizsgálja. Nem a release nevéből
próbálja kitalálni, hogy egy torrent RAR-os vagy ISO-s.

> [!IMPORTANT]
> A qBittorrent soha nem kap adattörlési utasítást. Torrent eltávolításakor a
> program kötelezően deleteFiles=false értéket küld. A RAR- és ISO-fájlokat
> kizárólag maga a Python-program törli, és csak a sikeres feldolgozás után.

## A két mód közötti legfontosabb különbség

A RAR- és az ISO-feldolgozást szándékosan nem lehet egyetlen menetben
elindítani. Ennek az az oka, hogy egy RAR-készlet kibontott eredménye maga is
lehet ISO-fájl.

Az ajánlott sorrend:

1. RAR-próbaüzem.
2. RAR-feldolgozás.
3. Várd meg, amíg a RAR-feldolgozás teljesen befejeződik.
4. ISO-próbaüzem.
5. ISO-feldolgozás.

Soha ne indítsd egyszerre a két parancsot. A közös zárolás ezt technikailag is
megakadályozza.

## Pontosan hogyan kezeli a különböző eseteket?

| Helyzet | ISO feldolgozása | qBittorrent-művelet |
|---|---|---|
| Az ISO korábban egy RAR-ból lett kibontva, és a RAR torrentje már nincs a listán | Igen | Nincs listaelem, ezért nincs qBittorrent-művelet |
| Az ISO eleve egy kész torrent fájlja | Igen | Siker után leállítás, majd csak listaeltávolítás deleteFiles=false értékkel |
| Az ISO egyáltalán nem szerepel a torrentlistán | Igen | Nincs qBittorrent-művelet |
| A release-mappában már teljes BDMV-struktúra van | Az ISO tartalmát nem másolja ki újra; csak az ISO-t törli | Ha van natív ISO-torrent, csak a listáról eltávolítja |
| Ugyanabban a mappában a BDMV és az ISO két külön torrentből származik | A meglévő teljes BDMV változatlan marad, az ISO törlődik | Kizárólag az ISO pontos fájlútvonalát tartalmazó torrent kerül le a listáról; a BDMV-torrent megmarad |
| A release-mappában részleges vagy kétes BDMV van | Sikeres mount, másolás és unmount után a régi BDMV-t törli, majd ISO-ból újraépíti | A natív ISO-torrentet siker után csak a listáról eltávolítja |
| Az ISO-t birtokló torrent még nem 100%-os | Nem | A torrent és az ISO változatlan marad |
| Ugyanazon torrent másik ISO-fájlja hiányzik | Nem | A teljes torrent változatlan marad |
| Az ISO-t vagy RAR-t aither.cc trackerhez tartozó seed birtokolja | Nem | A seed és minden fájlja változatlan marad |

Ez valósítja meg azt a fontos logikát, hogy a RAR-ból létrejött ISO akkor is
feldolgozható, ha már nincs hozzá torrent, a natív ISO-torrent viszont siker
után eltávolítható a listáról.

## RAR-mód: mit csinál?

1. Rekurzívan átvizsgálja a telepítéskor megadott storage mappát.
2. A tényleges fájlnevek és kötetek alapján felismeri a RAR-készleteket.
3. Lekéri a qBittorrent összes torrentjének pontos fájllistáját.
4. Teljes elérési út alapján egyezteti a lemezen talált RAR-köteteket.
5. Csak 100%-os, teljesen és egyértelműen egyező torrentet dolgoz fel.
6. Az UnRAR tesztparancsával ellenőrzi az archívumot.
7. Ideiglenes munkamappába kibontja és ellenőrzi a tartalmat.
8. A kibontott fájlokat abba a mappába helyezi, ahol a RAR-kötetek vannak.
9. Leállítja az érintett torrentet vagy cross-seed torrenteket.
10. A RAR-köteteket átmeneti karanténba mozgatja.
11. A torrenteket deleteFiles=false értékkel eltávolítja a listáról.
12. A program törli a karanténba helyezett RAR-köteteket.

A RAR-mód csak olyan RAR-készletet dolgoz fel, amelyet pontosan hozzá tud
rendelni a qBittorrent-lista megfelelő kész torrentjéhez. Torrent nélküli RAR
nem kerül automatikusan törlésre.

### Felismert RAR-formátumok

Példák:

~~~text
release.rar
release.rar + release.r00 + release.r01 + ...
release.part01.rar + release.part02.rar + ...
~~~

Az árva folytatóköteteket, hiányos készleteket és részleges tulajdonosi
egyezést a program kihagyja.

## ISO-mód: mit csinál?

Az ISO-mód kizárólag a qbit-iso-cleaner paranccsal indul.

Minden feldolgozható ISO-nál a sorrend:

1. Lemezről, rekurzívan megtalálja a valódi .iso vagy .ISO fájlt.
2. Pontos fájlútvonal alapján megnézi, szerepel-e valamely torrentben.
3. Megnézi, van-e már teljes BDMV-struktúra az ISO saját mappájában.
4. Ha teljes BDMV van, kihagyja a mountot és a másolást, majd a későbbi
   biztonsági lépések szerint csak az ISO-t törli.
5. Ha részleges vagy kétes BDMV van, megjelöli cserére, de ekkor még nem
   törli.
6. Ha nincs teljes BDMV, az ISO-t csak olvasható módban, loop eszközzel
   mountolja.
7. A mount a következő biztonsági opciókat használja:
   ro, nosuid, nodev és noexec.
8. Ellenőrzi, hogy a mountolt tartalom csak szabályos fájlokat és mappákat
   tartalmaz-e. Szimbolikus linket és speciális fájlt elutasít.
9. A teljes tartalmat, a rejtett fájlokkal és üres mappákkal együtt ideiglenes
   munkamappába másolja.
10. Ellenőrzi az esetleges célfájl-ütközéseket.
11. Unmountolja az ISO-t.
12. Ellenőrzi, hogy az ISO-ból kimásolt tartalom valóban teljes új
   gyökérszintű BDMV-struktúrát tartalmaz-e.
13. Csak a sikeres másolás, unmount és BDMV-ellenőrzés után távolítja el a
   régi részleges vagy kétes BDMV-bejegyzést.
14. Ezután helyezi a kimásolt tartalmat az ISO saját
   mappájába, vagyis oda, ahol maga az ISO-fájl található.
15. Ha az ISO-t torrent birtokolja, leállítja a torrentet.
16. Az eredeti ISO-t átmeneti karanténba mozgatja.
17. Ha volt hozzá torrent, deleteFiles=false értékkel eltávolítja a listáról.
18. A program törli a karanténba helyezett eredeti ISO-fájlt.

Az ISO mount egy külön, privát Linux mount namespace-ben fut. Ha a folyamat
váratlanul megszűnik, a mount nem marad tartósan a rendszer globális mountjai
között. A program normál esetben ettől függetlenül mindig explicit unmountot
végez.

### Hova kerül az ISO tartalma?

Példa:

~~~text
/home/accofil/storage/Film.Release/disc.iso
~~~

Ha az ISO gyökerében BDMV mappa található, a cél ez lesz:

~~~text
/home/accofil/storage/Film.Release/BDMV/
~~~

Nem jön létre egy további disc vagy mount_iso szint. Az ISO gyökerének
tartalma közvetlenül az ISO-t tartalmazó release-mappába kerül.

Az új ISO-kimeneti fájlok tulajdonosa a telepítéskor megadott qBittorrent
Linux-felhasználó lesz. Új mappák 0755, új fájlok 0644 jogosultságot kapnak.
A program nem használ chmod 777 beállítást.

### Mikor számít már meglévőnek a BDMV-struktúra?

A gyors, csak ISO-törlést végző ág kizárólag akkor aktiválódik, ha az ISO
saját release-mappájában egy valódi BDMV könyvtár található, benne legalább:

- index.bdmv fájl;
- MovieObject.bdmv fájl;
- PLAYLIST könyvtár;
- CLIPINF könyvtár;
- STREAM könyvtár.

A kis- és nagybetű nem számít. Szimbolikus link, hiányzó elem vagy csak egy
üres BDMV mappa nem elég. Ilyenkor a program először a normál mount–másolás és
unmount ellenőrzést végzi el. Csak ennek sikere után törli a régi részleges
vagy kétes BDMV-t, és az ISO-ból kimásolt új struktúrát teszi a helyére. Ha a
mount, másolás vagy unmount hibázik, a régi BDMV és az ISO is megmarad.
Ugyanez történik akkor is, ha az ISO-ban nem található teljes gyökérszintű
BDMV: a részleges régi struktúrát ilyenkor nem törli.

Teljes BDMV esetén a program nem hasonlítja újra össze a lemez tartalmát: a
kérés szerint csak az ISO-t törli.

### Két külön torrent ugyanabban a release-mappában

Ha az egyik torrentoldal BDMV-struktúrát, a másik pedig ISO-fájlt adott ugyanabba
a release-mappába, a program nem a közös mappa alapján választ torrentet. Minden
qBittorrent-fájllistát külön, teljes fájlútvonal alapján ellenőriz:

- a BDMV fájljait tartalmazó torrent változatlanul seedben marad;
- csak az a torrent áll le és kerül le a listáról, amelynek fájllistájában a
  törlendő ISO pontos útvonala szerepel;
- ha egyetlen torrent fájllistájában sincs benne az ISO, csak az ISO törlődik;
- ha ugyanazt az ISO-fájlt több cross-seed torrent is pontosan tartalmazza,
  mindegyik ilyen ISO-tulajdonos együtt kerül csak-listás eltávolításra.

A qBittorrent itt is kötelezően deleteFiles=false értéket kap; a BDMV adatait
sem a kliens, sem a program nem törli.

## Fájlütközések

Mindkét mód ugyanazt a szabályt alkalmazza:

- ha a célfájl még nem létezik, létrehozza;
- ha azonos nevű és bájtra azonos tartalmú fájl már létezik, SHA-256
  ellenőrzés után elfogadja;
- ha azonos nevű, de eltérő tartalmú fájl létezik, a teljes feldolgozási
  egységet leállítja;
- eltérő fájlt soha nem ír felül.

Az azonos fájl fizikai felülírása szükségtelen, mert a végeredmény ugyanaz.
A program ezért az ellenőrzött, már meglévő példányt tartja meg.

## Védett aither.cc seedek

A program lekéri az érintett torrent összes trackerét. Ha bármelyik tracker
hostneve aither.cc vagy annak aldomainje, akkor:

- nem törli a RAR- vagy ISO-fájlt;
- nem állítja le végleg a seedet;
- nem távolítja el a torrentlistáról;
- cross-seed esetén az egész közös feldolgozási egységet kihagyja.

Hasonló nevű idegen domainek, például aither.cc.example.org, nem számítanak
aither.cc trackernek.

## Biztonsági működés

- Minden parancs alapértelmezés szerint próbaüzem.
- Próbaüzemben nincs mount, fájlművelet vagy qBittorrent-módosítás.
- A valódi futáshoz külön --apply kapcsoló és kézi megerősítő szó kell.
- A fájlrendszer a felismerés elsődleges forrása.
- A qBittorrent egyeztetés pontos fájlútvonal alapján történik.
- Nem 100%-os torrent fájlját nem törli.
- Cross-seedeket egy tranzakcióban kezel.
- Hibás vagy hiányos RAR-készletet nem töröl.
- Sikertelen ISO-másolás vagy unmount után az ISO megmarad.
- Már meglévő teljes BDMV esetén az ISO mount és újramásolás nélkül törölhető.
- Unmount-hiba esetén az ISO-feldolgozás azonnal megszakad.
- A scan rooton kívüli elérési utat nem kezel.
- Szimbolikus linket és útvonalbejárást elutasít.
- Külön ellenőrzi az ideiglenes munkameghajtó és a storage szabad helyét.
- Legalább 1 GiB biztonsági tartalékot hagy.
- Egyszerre csak egy RAR- vagy ISO-folyamat futhat.
- A qBittorrent törlési API-hívás minden esetben deleteFiles=false.

Az ideiglenes kibontás az NVMe-n található következő mappát használja:

~~~text
/home/accofil/.cache/qbit-rar-cleaner-work
~~~

A pontos home útvonal a választott Linux-felhasználótól függ. A törlés előtti
karantén ezzel szemben a storage fájlrendszerén marad. Ez szükséges, mert az
NVMe XFS és a storage ext4 fájlrendszere között az atomi os.replace művelet
nem működik.

## Követelmények

- Debian 12 vagy kompatibilis Debian/Ubuntu rendszer;
- sudo jogosultság a telepítéshez és az ISO-módhoz;
- Python 3 és python3-venv;
- qBittorrent natív WebUI/API;
- qBittorrent WebUI URL, felhasználónév és jelszó;
- a qBittorrent Linux-felhasználójának teljes hozzáférése a scan roothoz;
- a RAR-módhoz a natív `unrar` program;
- util-linux csomag a mount, umount és unshare parancsokhoz;
- loop eszköz és ISO9660/UDF támogatás a Linux kernelben;
- internetkapcsolat a telepítéshez szükséges csomagok letöltéséhez.

A telepítő megpróbálja telepíteni a hiányzó Python-, util-linux- és `unrar`
csomagokat. A RAR-támogatást nem verziószöveg alapján, hanem egy valódi, apró
RAR-próbaarchívum megnyitásával ellenőrzi. A natív `unrar` használata elkerüli
az eltérő verziójú 7-Zip motor és külső RAR-plugin okozta összeomlásokat.

Ha a RAR-támogatás ennek ellenére sem érhető el, a telepítő ezt világosan
jelzi, de nem áll meg az ISO-parancs telepítése előtt. Ilyenkor a
`qbit-iso-cleaner` használható; a `qbit-rar-cleaner` biztonságos
hibaüzenettel leáll, amíg a RAR-csomagokat nem javítod és a telepítőt újra
nem futtatod.

> [!NOTE]
> Debian 12 alatt az unrar csomaghoz szükség lehet a non-free
> csomagtároló engedélyezésére.

## Telepítés kezdőknek

### 1. Jelentkezz be SSH-val

A saját szervercímedet használd:

~~~bash
ssh accofil@SZERVER_CIME
~~~

Ha már a szerver parancssorát látod, folytasd.

### 2. Töltsd le a külön projektet

~~~bash
cd ~
git clone https://github.com/accofil/qbit-rar-cleaner.git
cd qbit-rar-cleaner
~~~

Ha a git parancs hiányzik:

~~~bash
sudo apt update
sudo apt install -y git
~~~

Ezután ismét futtasd a git clone parancsot.

### 3. Indítsd el a telepítőt

~~~bash
sudo bash install.sh
~~~

Ne adj külön `chmod +x` parancsot az `install.sh` fájlra. A
`sudo bash install.sh` végrehajtási jog nélkül is működik, és nem jelöli
helyileg módosítottnak a Git által követett fájlt.

A telepítő öt adatot kér. A szögletes zárójelben látható érték az
alapértelmezés. Ha megfelelő, csak nyomj Entert.

#### A qBittorrent Linux-felhasználója

Az a szerverfelhasználó, amely alatt a qBittorrent fut. Ennél a szervernél a
jellemző érték:

~~~text
accofil
~~~

#### Átvizsgálandó qBittorrent save path

A legfelső storage mappa, amely alatt RAR-okat és ISO-kat kell keresni:

~~~text
/home/accofil/storage
~~~

Az útvonalnak már léteznie kell.

#### qBittorrent WebUI URL

Ha a qBittorrent ugyanazon a szerveren fut:

~~~text
http://127.0.0.1:10987
~~~

A tényleges portot add meg. Csak a portszámot is beírhatod:

~~~text
10987
~~~

A telepítő ezt automatikusan helyi HTTP URL-re alakítja.

#### qBittorrent WebUI felhasználónév

A WebUI bejelentkezési neve. Nem feltétlenül azonos a Linux-felhasználóval.

#### qBittorrent WebUI jelszó

Gépelés közben sem karakter, sem csillag nem jelenik meg. Ez normális. Írd be
a jelszót, majd nyomj Entert.

### 4. Nézd át az automatikus RAR-próbaüzemet

A telepítő a végén kizárólag RAR-próbaüzemet indít. Ez nem változtat meg
semmit. Ha nincs működő RAR-kibontó, ezt a próbaüzemet kihagyja, de az
ISO-parancsot akkor is telepíti.

Első telepítéskor erre a kérdésre válaszolj N-nel, vagy csak nyomj Entert:

~~~text
Elindítsam most a valódi RAR-feldolgozást? [y/N]:
~~~

Az ISO-mód telepítéskor nem indul el automatikusan.

## Használat telepítés után

### 1. RAR-próbaüzem

~~~bash
sudo qbit-rar-cleaner
~~~

A végén ezt kell látnod:

~~~text
PRÓBAÜZEM: nem történt fájlművelet és qBittorrent-módosítás.
~~~

### 2. Valódi RAR-feldolgozás

~~~bash
sudo qbit-rar-cleaner --apply
~~~

A program ezt a pontos megerősítő szót kéri:

~~~text
FELDOLGOZOM
~~~

Várd meg a parancs teljes befejeződését. Ne indíts közben ISO-feldolgozást.

### 3. ISO-próbaüzem

Csak a RAR-futás befejezése után:

~~~bash
sudo qbit-iso-cleaner
~~~

Próbaüzemben a program az ISO-kat sem mountolja.

A listában minden ISO-nál külön látható:

- a teljes ISO-útvonal;
- az ISO mérete;
- a hozzá tartozó qBittorrent torrent neve; vagy
- az, hogy nincs hozzá listaelem, de ettől még feldolgozható.

### 4. Valódi ISO-feldolgozás

~~~bash
sudo qbit-iso-cleaner --apply
~~~

A program ezt a pontos megerősítő szót kéri:

~~~text
ISO_FELDOLGOZOM
~~~

Más szöveg esetén módosítás nélkül leáll.

### Felügyelet nélküli futtatás

RAR:

~~~bash
sudo qbit-rar-cleaner --apply --yes
~~~

ISO:

~~~bash
sudo qbit-iso-cleaner --apply --yes
~~~

A --yes kihagyja a megerősítő kérdést. Kezdőknek és első futtatáshoz nem
ajánlott. Előtte mindig futtasd és olvasd végig a megfelelő próbaüzemet.

## Mi marad meg a feldolgozás után?

### RAR siker után

- a kibontott tartalom a RAR-kötetek eredeti mappájában;
- a nem RAR fájlok, például NFO, SFV, kép és sample;
- a RAR-kötetek már nem;
- a megfelelő torrentlista-bejegyzés már nem;
- a qBittorrent semmilyen adatfájlt nem töröl.

### ISO siker után

- az ISO gyökerének teljes tartalma az ISO eredeti mappájában;
- a release többi meglévő fájlja;
- az eredeti ISO már nem;
- ha már teljes BDMV volt a mappában, az változatlanul megmarad, és nem történik
  újabb ISO-kimásolás;
- natív ISO-torrent esetén a torrentlista-bejegyzés már nem;
- RAR-ból keletkezett, torrent nélküli ISO esetén nincs qBittorrent-művelet.

### Hiba vagy kihagyás után

- az eredeti RAR vagy ISO megmarad;
- a torrentlista-bejegyzés megmarad;
- az eredetileg futó torrentet a program megpróbálja újraindítani;
- már sikeresen létrejött, azonos kimeneti fájlok megmaradhatnak, így a futás
  később biztonságosan folytatható.

## Telepített fájlok

| Elem | Hely |
|---|---|
| Program és Python-környezet | /opt/qbit-rar-cleaner/ |
| RAR-parancs | /usr/local/bin/qbit-rar-cleaner |
| ISO-parancs | /usr/local/bin/qbit-iso-cleaner |
| Beállítások | ~/.config/qbit-rar-cleaner/config.json |
| NVMe munkamappa | ~/.cache/qbit-rar-cleaner-work/ |
| Átmeneti storage-karantén | &lt;scan_root&gt;/.qbit-rar-cleaner-quarantine/ |

A konfigurációs mappa 0700, a konfigurációs fájl 0600 jogosultságot kap. A
qBittorrent jelszava az API-bejelentkezés miatt szükséges, ezért ebben a
védett fájlban tárolódik. Nem kerül a GitHubra és normál futáskor nem jelenik
meg.

## Frissítés

Először győződj meg róla, hogy sem RAR-, sem ISO-feldolgozás nem fut. Ezután:

~~~bash
cd ~/qbit-rar-cleaner
cp -a install.sh ~/qbit-rar-cleaner-install.sh.backup
git restore -- install.sh
git pull --ff-only
sudo bash install.sh
~~~

A telepítő frissíti mindkét parancsot és újra bekéri a beállításokat. A végén
ismét csak RAR-próbaüzemet indít.

A `cp` egy biztonsági másolatot készít a helyi telepítőfájlról. A
`git restore` csak a projektben lévő `install.sh` helyi eltérését állítja
vissza; a storage mappához, a torrentekhez és a telepített konfigurációhoz
nem nyúl.

Ne frissítsd a telepített programot egy még futó UnRAR-kibontás vagy
ISO-másolás közben.

## Gyakori hibák

### git: command not found

~~~bash
sudo apt update
sudo apt install -y git
~~~

### A git pull ezt írja: would be overwritten by merge: install.sh

Ez általában azért történik, mert korábban `chmod +x install.sh` futott, és a
Git ezt helyi módosításként látja. Futtasd pontosan ezt:

~~~bash
cd ~/qbit-rar-cleaner
cp -a install.sh ~/qbit-rar-cleaner-install.sh.backup
git restore -- install.sh
git pull --ff-only
sudo bash install.sh
~~~

Ez nem töröl storage-adatot és nem módosít qBittorrent torrentet.

### sudo: qbit-iso-cleaner: command not found

Ez azt jelenti, hogy a telepítő még azelőtt állt le, hogy létrehozta volna a
`/usr/local/bin/qbit-iso-cleaner` parancsot. Frissítsd a projektet az előző
pont parancsaival, majd futtasd végig a telepítőt. A javított telepítő akkor
is létrehozza az ISO-parancsot, ha a RAR-támogatás külön javításra szorul.

### A qBittorrent jelszó nem lehet üres

Indítsd újra a telepítőt. A jelszó gépelés közben láthatatlan.

### Nincs teljes hozzáférés a scan roothoz

Ellenőrizd a Linux-felhasználót és a save pathot. Ne használj találomra
chmod 777 parancsot.

### A qBittorrent bejelentkezés sikertelen

Ellenőrizd a WebUI URL-t, portot, felhasználónevet és jelszót. Helyi
kapcsolatnál általában 127.0.0.1 szerepel a címben.

### HIBA: Invalid IPv6 URL

Az üzenetnek két lehetséges forrása volt a régebbi verziókban:

- hibásan beírt WebUI-cím, például egy felesleges `[` vagy `]` karakter;
- valamelyik torrent qBittorrentben tárolt, hibás IPv6-formátumú tracker-URL-je.

A jelenlegi verzió a WebUI-címet mentés előtt szigorúan ellenőrzi. Egy
értelmezhetetlen tracker-címet nem tekint védett domain-egyezésnek, de emiatt
nem állítja le a teljes lemezvizsgálatot. Frissítsd a projektet, majd futtasd
újra a telepítőt:

~~~bash
cd ~/qbit-rar-cleaner
git pull --ff-only
sudo bash install.sh
~~~

A WebUI URL kérdésénél a helyi qBittorrenthez elég csak a portot beírni:

~~~text
10987
~~~

A telepítő ezt `http://127.0.0.1:10987` címre alakítja, szigorúan ellenőrzi,
és hibás zárójel, port, útvonal vagy beágyazott jelszó esetén még a
konfiguráció mentése előtt leáll.

### Az UnRAR nem tudja megnyitni a RAR-próbaarchívumot

Debian 12 alatt engedélyezd a non-free csomagtárolót, majd telepítsd az
`unrar` csomagot:

~~~bash
sudo apt update
sudo apt install -y unrar
~~~

Ezután futtasd újra:

~~~bash
cd ~/qbit-rar-cleaner
sudo bash install.sh
~~~

A telepítő tényleges RAR-megnyitási próbát végez, ezért egy eltérően kiírt
`Rar`, `Rar5` vagy más codec-név önmagában már nem okoz téves hibát.

### Egy másik RAR/ISO-feldolgozás már fut

Várd meg a másik parancs befejeződését. A RAR- és ISO-mód nem futhat egyszerre.

### Az ISO mountolása sikertelen

Ellenőrizd, hogy valóban sudo paranccsal indítottad-e:

~~~bash
sudo qbit-iso-cleaner --apply
~~~

Ha az üzenet loop eszközt, ISO9660/UDF támogatást vagy Operation not permitted
hibát említ, akkor a szerverkörnyezet nem engedélyezi a szükséges mount
műveletet. Ilyenkor a program az ISO-t nem törli.

### Az ISO unmount sikertelen

A program azonnal megszakítja a további ISO-feldolgozást, és nem törli az
érintett ISO-t. A privát mount namespace a folyamat kilépésekor megszűnik.
Új futás előtt olvasd el a teljes hibaüzenetet.

### Eltérő tartalmú célfájl már létezik

A release mappájában ugyanazon az útvonalon más tartalmú fájl található. A
program szándékosan nem írja felül. Az eredeti RAR vagy ISO és a torrent
megmarad.

### Kihagyott védett tracker

Az érintett torrent valamelyik trackerének hostneve aither.cc. Ez szándékos
védelem; a program nem bontja meg ezt a seedet.

### Nincs feldolgozható RAR

Ez nem feltétlenül hiba. Lehet, hogy nincs RAR, a készlet hiányos, a torrent
nem 100%-os, nincs pontos qBittorrent-egyezés, vagy védett aither.cc seedről
van szó.

### Nincs feldolgozható ISO

Lehet, hogy nincs ISO, a natív ISO-torrent nem 100%-os, hiányzik ugyanannak a
torrentnek egy másik ISO-ja, vagy védett seedről van szó. A torrent nélküli,
szabályos ISO-k ettől eltérően feldolgozhatók.

## Fejlesztői ellenőrzés

~~~bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile qbit_rar_cleaner.py
bash -n install.sh
~~~

A tesztek ellenőrzik többek között:

- a fájlrendszer-alapú RAR- és ISO-felismerést;
- a pontos qBittorrent-fájlútvonal-egyezést;
- a torrent nélküli RAR-ból keletkezett ISO kezelését;
- a natív ISO-torrent listaeltávolítását;
- a több ISO-t tartalmazó torrent védelmét;
- a nem 100%-os torrentek kihagyását;
- az aither.cc tracker védelmét;
- az ütközési és útvonalbiztonsági szabályokat;
- az unmount kötelező sorrendjét;
- azt, hogy a qBittorrent törlési kérés mindig deleteFiles=false.
