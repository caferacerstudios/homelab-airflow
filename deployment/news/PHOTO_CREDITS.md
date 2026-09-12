# Daily article photo credits

The daily article task obtains photo credits from the Airflow Variable
`fan_zone_photo_credits`. It reads this Variable when each team's task executes,
independently of `fan_zone_active_sites`. The active-sites configuration continues
to select the team, photo directory and article output directory.

Credits come from the owner's photo metadata. The article-writing model does not
write, infer or change the photographer credit.

## Initial configuration

After installing this source update, import
`config/photo-credits-airflow-import.json` using the Variables import action in
Airflow at `http://localhost:8085/variables`.

There are two JSON files with different purposes:

| File | Contents | Use |
| --- | --- | --- |
| `config/photo-credits.json` | Raw catalog with `schema_version`, `assets` and `sha256` | Shared validator, manual credit update and checked-in fallback |
| `config/photo-credits-airflow-import.json` | An object whose `fan_zone_photo_credits` key contains that catalog | Airflow Variables JSON import |

When editing the individual Variable's value, paste the **raw catalog**, without
the outer `fan_zone_photo_credits` wrapper. Do not upload the Getty CSV directly
to the Variables JSON importer: first convert it using the script below.

The supplied catalog contains the five Getty assets reviewed for this update.
It extracts the exact `Photo by …/Getty Images` attribution from the CSV's
**Caption** field. The CSV's Contributor value, such as `Getty Images Sport`,
is an agency category and is not substituted for the photographer's name.
Download account details, downloader name, subscription information, download
IDs and project notes are excluded from the catalog and published articles.

If the Variable is absent, tasks use the checked-in raw catalog. An explicitly
empty catalog (`{"schema_version":1,"assets":{},"sha256":{}}`) is respected;
it does not restore the checked-in mappings automatically.

## Adding later downloads

Run these commands as `laurawkr` from `/home/laurawkr/homelab-airflow`, replacing
the CSV path with the file you downloaded:

```bash
python3 deployment/news/convert_photo_credits.py \
  /home/laurawkr/getty-downloads.csv \
  --existing config/photo-credits.json \
  --output /home/laurawkr/photo-credits.json

python3 deployment/news/convert_photo_credits.py \
  /home/laurawkr/getty-downloads.csv \
  --existing /home/laurawkr/photo-credits.json \
  --airflow-import \
  --output /home/laurawkr/photo-credits-airflow-import.json
```

Import the new `photo-credits-airflow-import.json` in Airflow. The first command
also gives you the raw catalog needed by the manual update command below.
If your current Variable already contains additions beyond the checked-in
catalog, save its raw value and use that file for the first `--existing` argument.
This preserves those assets and approved SHA256 mappings. Review and commit the
updated raw catalog and import wrapper when updating the checked-in fallback.

The converter skips canceled downloads and non-photo rows. It requires an
explicit photographer/Getty Images credit, rejects conflicting duplicate asset
rows, and retains the supplied editorial caption. An unambiguous event date in
the caption becomes `eventDate`; the download date is not used as the event date.
Conversion does not upload image files. Continue putting each team's images in
that team's configured photo directory.

## How a photo is matched

An image receives an approved Getty attribution through one of these explicit
identities:

- A Getty asset number in the filename, such as `gettyimages-2260614489.jpg`.
  Numeric boundaries prevent matching that number inside a longer number.
- Its SHA256 digest in the catalog's `sha256` mapping. This supports existing
  renamed images whose bytes have been verified against an asset.
- A `gettyAssetId` in the photo directory's existing `metadata.json` entry for
  that filename.

The referenced asset must exist in the catalog. Conflicting identities exclude
the photo; matching does not guess from a player's name, article topic or visual
similarity. Existing owner-supplied `caption` and `credit` metadata remains
supported for manually credited images. An unknown photo is reported and skipped.
If no unused, credited image is available, generation uses the existing neutral
Fan Zone illustration.

The selection still excludes images used by the seven visible stories and
deduplicates identical image bytes. Selected images remain immutable in the news
assets and releases. The hero stores the formatted caption, credit, SHA256 and,
when supplied, Getty asset ID and event date. When the event predates the article,
the caption includes `File photo.` and ends with one photographer credit.

The validator enforces the website limits of 2,000 characters for the complete
caption and credit, and 1,000 characters for explicit alt text. The normalized
catalog is limited to **22,000 UTF-8 bytes**; the entire encoded news request must
also fit the existing 24,576-byte transport limit. An oversized or invalid
Variable fails validation before the task writes a new article.

## Updating photos on already accepted articles

Installing the code or importing the Variable does not rewrite previously
accepted articles. To update the existing Seahawks article photo metadata after
the source update is installed, run without `sudo`:

```bash
cd /home/laurawkr/homelab-airflow

python3 deployment/news/apply_photo_credits.py \
  --team seahawks --credits-file config/photo-credits.json --check

python3 deployment/news/apply_photo_credits.py \
  --team seahawks --credits-file config/photo-credits.json --apply
```

For a later catalog, replace `--credits-file` with the new raw JSON file. For
another active team, change `--team`. The default operation is the read-only
check; `--apply` backs up accepted article files, updates verified photo metadata,
and publishes a new release under the same news runtime. It does not regenerate
article text or make API calls. Unmatched existing photos are retained and
reported for review; they are not assigned a guessed credit. Previous releases,
image bytes, article identities and publication dates are retained.

The normal daily task saves its validated incoming catalog to
`<news-runtime>/photo-credits.json` while holding the existing generation lock.
This supports host-runner reuse. The manual credit update uses its explicit
`--credits-file`, or the checked-in catalog when that argument is omitted.

## Website builds and output locations

This update does not move article feeds. Seahawks articles remain at
`/var/lib/sfz-news/current`, Broncos articles remain at
`/var/lib/boncosfz-news/current`, and additional teams retain the locations in
`fan_zone_active_sites`. The existing `boncosfz` spelling is intentional for
compatibility with the installed paths.

After applying a credit update, rebuild the selected website. Both
`/home/laurawkr/seahawksfanzone` and `/home/laurawkr/templatefanzone` import the
same selected-team article snapshot and display its credited hero caption.
Neither build obtains photo credits from Airflow directly. No Airflow restart,
DAG reinstall or article-generation rerun is needed for this credit update.
