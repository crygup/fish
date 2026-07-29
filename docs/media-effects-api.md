# Fishie media effects API

The media effects endpoint applies the same renderer used by Fishie's effect
commands. Requests require the private API key configured as `keys.media_api`
in `config.toml`.

## Authentication

Send the key in the `X-API-Key` request header. Do not put the key in a URL,
frontend JavaScript, or a public repository.

## List effects

```http
GET /fishie/media/effects
```

This returns the supported effect names and the current 50 MB input limit.

Bundled sound effects have their own authenticated catalog:

```http
GET /fishie/media/audio-effects
X-API-Key: your-private-key
```

Each entry includes its stable ID, normalized name, category, and duration.

## Apply an effect to a URL

```http
POST /fishie/media/effects/{effect}
Content-Type: application/json
X-API-Key: your-private-key

{
  "media_url": "https://example.com/image.png",
  "secondary_media_url": "https://example.com/optional-audio.mp3",
  "options": {
    "radius": 8,
    "blur_type": "motion"
  }
}
```

`secondary_media_url` is used by `audiooverlay` and `audioreplace`. It can
point to an audio file or a video that contains audio.

The URL must use HTTP or HTTPS, resolve to a public address, and return an
image, video, or audio content type. Redirects are validated before they are
followed.

## Apply an effect to an uploaded file

Upload the file as the raw request body. Pass effect options as a JSON object
in the `options` query parameter.

```bash
curl \
  -X POST \
  -H "X-API-Key: your-private-key" \
  -H "Content-Type: image/png" \
  --data-binary "@input.png" \
  "https://api.crygup.com/fishie/media/effects/pixelate?options=%7B%22size%22%3A16%7D" \
  --output pixelate.png
```

The response body is the finished file. `Content-Type` and
`Content-Disposition` describe its generated format and filename.
Each effect has a 30 second processing limit.

## Parameters and types

Each effect accepts the same parameter names as its Fishie command flags
without the leading dash. Values must be JSON strings, finite numbers, or
booleans.

| Effect | Parameters |
| --- | --- |
| `invert` | `preserve_transparency` boolean |
| `flip` | `direction` string: `horizontal` or `vertical` |
| `blur` | `radius` number from 0.1 to 50, `blur_type` string: `gaussian`, `box`, or `motion` |
| `crop` | `shape` string: `circle` or `triangle` |
| `deepfry` | `intensity` number from 0.25 to 3, `preserve_transparency` boolean |
| `grayscale` | `preserve_transparency` boolean |
| `mirror` | `direction` string: `top`, `right`, `bottom`, or `left` |
| `jpeg` | `quality` integer from 1 to 50 |
| `spin` | `speed` number from 0.25 to 4, `clockwise` boolean |
| `magik` | `strength` number from 1 to 80 |
| `gifmagik` | `strength` number from 1 to 80, `speed` number from 0.25 to 4 |
| `swirl` | `strength` number from -720 to 720 |
| `gifswirl` | `strength` number from -720 to 720, `speed` number from 0.25 to 4 |
| `wiggle` | `amount` number from 1 to 30, `speed` number from 0.25 to 4 |
| `cube`, `pyramid` | `speed` number from 0.25 to 4, `clockwise` boolean |
| `fadein`, `fadeout` | `duration` number from 0.1 to 10 seconds |
| `lag` | `amount` integer from 2 to 12, `method` string: `random`, `freeze`, `stutter`, `drop`, or `jitter`, `multi` boolean |
| `shuffle` | No parameters |
| `tint` | `color` CSS color or hex string, `amount` number from 0 to 1 |
| `implode`, `explode`, `fisheye` | `strength` number from 0 to 1 |
| `sharpen` | `amount` number from 0 to 10 |
| `legoify` | `size` integer from 3 to 64 |
| `bounce` | `amount` number from 1 to 100, `speed` number from 0.25 to 4 |
| `sepia` | `amount` number from 0 to 1 |
| `pixelate` | `size` integer from 2 to 128 |
| `slidein`, `slideout` | `direction` string: `left`, `right`, `up`, or `down`, `duration` number from 0.1 to 10 |
| `vignette` | `amount` number from 0 to 1 |
| `resize` | `scale` number from 0.1 to 4, `ratio` string such as `16:9` or `1:1` |
| `distort` | `amount` number from -1 to 1 |
| `grain`, `noise` | `amount` number from 0 to 100 |
| `rotate` | `degrees` number from -3600 to 3600 |
| `brightness`, `contrast`, `saturation` | `amount` number from 0 to 4 |
| `exposure` | `stops` number from -5 to 5 |
| `hallway`, `parallax` | `speed` number from 0.25 to 4 |
| `huerotate` | `degrees` number from -3600 to 3600 |
| `zoom` | `amount` number from 1 to 4, `forever` boolean |
| `squishy` | `amount` number from 0 to 1 |
| `glitch` | `amount` number from 0 to 100 |
| `tremble` | `amount` number from 1 to 30 |
| `quilt` | `tiles` integer from 2 to 12 |
| `removebars`, `removecaption` | No parameters |
| `removeoutrotiktok`, `removeoutroreels` | No parameters. The outro is removed only when its ending transition is confidently detected |
| `enlarge` | `amount` number from 1 to 4 |
| `falsecolor`, `watercolor`, `oilpaint`, `random` | No parameters |
| `meme` | `text` string |
| `volume` | `volume` number from 0 to 10 and the common audio timing fields |
| `bassboost`, `basslower` | `gain` number from 1 to 30 and the common audio timing fields |
| `audioreverse` | Common audio timing fields |
| `audioreverb` | `room` number from 0.1 to 1 and the common audio timing fields |
| `audiodestroy` | `amount` integer from 2 to 12 and the common audio timing fields |
| `audiocompress` | `ratio` number from 1 to 20 and the common audio timing fields |
| `channelscombine`, `audiounderwater`, `audionightcore`, `audiodeepvoice`, `audiosurround`, `audioecho` | Common audio timing fields |
| `audiopitch` | `semitones` number from -12 to 12 and the common audio timing fields |
| `audiooverlay` | Requires `secondary_media_url`. Accepts `at`, `source_start`, `source_stop`, `duration`, `volume`, `pitch`, and `random_time` |
| `soundeffect` | Accepts `effect` as a catalog ID, name, or `random`. Also accepts `at`, `source_start`, `source_stop`, `duration`, `volume`, `pitch`, `speed`, `random_time`, `loop`, `fade_in`, and `fade_out`. `random_time` defaults to `true` |
| `adhd` | No parameters. Alternates random 1.15–3× faster sections with 0.5–0.9× slower sections and applies matching voice effects |
| `audioreplace` | Requires `secondary_media_url` |
| `extract` | No parameters. The response is an MP3 or ZIP file |

The common audio timing fields are `start`, `stop`, and `duration`. Each is
measured in seconds from 0 to 180. A `stop` or `duration` value of `0` means
the effect continues through the rest of the media. A positive `duration`
takes priority over `stop`.

Visual effects also accept `start` and `stop` when the input is a video. The
effect is applied only inside that window while the surrounding video remains
unchanged.

Numbers outside their documented range are clamped to the nearest supported
value. When this happens, the response includes an `X-Fishie-Adjusted` header
with the adjustments that were made.

`gifmagik` and `gifswirl` accept static images, GIFs up to 30 seconds, and
videos up to the general 3 minute media limit. Video processing uses a bounded
resolution and frame rate so longer clips do not use the much slower
frame-by-frame GIF renderer. Discord CDN attachment URLs are refreshed through
Discord before Fishie downloads them.

Invalid parameters, unsupported media, private-network URLs, oversized inputs,
processing timeouts, and effects that cannot process the supplied media return
a `4xx` response with a JSON `detail` message.
