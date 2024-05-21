import json
import os
import unicodedata
from typing import Dict, Union

import numpy as np
from fuzzywuzzy import fuzz
from scipy.optimize import linear_sum_assignment

import ExternalInfo.ThwikiInfoProvider.Output.path_definitions as ThwikiOutput
import Processor.InfoCollector.Aggregator.output.path_definitions as MergedOutput
from ExternalInfo.ThwikiInfoProvider.ThwikiOriginalTrackMapper.SongQuery import (
    SongQuery,
    get_original_song_query_params,
)
from ExternalInfo.ThwikiInfoProvider.ThwikiSongInfoProvider.Model.ThcSongInfoModel import (
    Album,
    Track,
)
from Shared import utils

merged_output_path = utils.get_output_path(MergedOutput, MergedOutput.ID_ASSIGNED_PATH)

album_formatted_output_path = utils.get_output_path(
    ThwikiOutput, ThwikiOutput.THWIKI_ALBUM_FORMAT_RESULT_OUTPUT
)
track_formatted_output_path = utils.get_output_path(
    ThwikiOutput, ThwikiOutput.THWIKI_TRACK_FORMAT_RESULT_OUTPUT
)
score_debug_output_path = utils.get_output_path(
    ThwikiOutput, ThwikiOutput.THWIKI_ALBUM_FORMAT_SCORE_DEBUG_OUTPUT
)

non_offical_works = {
    "地灵殿PH音乐名",
    "东方夏夜祭音乐名",
    "Cradle音乐名",
    "东方音焰火音乐名",
    "东方魔宝城音乐名",
    "8MPF音乐名",
    "东方梦旧市音乐名",
    "神魔讨绮传音乐名",
    "风神录PH音乐名",
    "TLM音乐名",
    "かごめかごめ",
}


def resolve_original_tracks(track: Track, abbriv_map: Dict):
    if not track.original:
        return []
    original = json.loads(track.original)
    query_params = get_original_song_query_params(original)
    org_tracks = []
    for qp in query_params:
        try:
            if qp[0] in non_offical_works:
                continue
            result = SongQuery.query(
                qp[0],
                qp[1],
            )
        except:
            print("\n\n[ERROR] Failed to resolve original track: {}\n\n".format(qp))
            continue
        abbriv = abbriv_map[result.source.id]
        index = result.index
        org_tracks.append(f"{abbriv}-{index}")
    return json.dumps(org_tracks)


def load_original_song_map() -> Dict:
    path = (
        input(
            "Enter path to CSV with original track abbrivations (Generated by get-original-alb-trk.py): "
        )
        or r"OriginalAlbums_Blank.csv"
    )
    if not os.path.isfile(path):
        print("Invalid path")
        exit(1)

    original_song_map = {}
    with open(path, "r", encoding="utf-8") as f:
        # skip header
        f.readline()
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            data = line.split(",")
            id = data[0]
            abbriv = data[2]
            original_song_map[id] = abbriv
            print("[{}] Loaded {} -> {}".format(idx + 1, id, abbriv))

    return original_song_map


def normalize_text(text):
    return "".join(
        [
            c
            for c in unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
            if unicodedata.category(c).startswith("L")
        ]
    ).lower()


def get_album_id(entry):
    return entry["AlbumMetadata"]["AlbumId"]


def collect_tracks(entry):
    all_tracks = []
    for _, disc in entry["Discs"].items():
        for track in disc["Tracks"]:
            all_tracks.append(track)

    return all_tracks


def match_src_thw_tracks_fuzzy(src_tracks, thc_tracks) -> Union[Dict[str, dict], None]:

    title_map = {
        normalize_text(json.loads(track.title_jp)[0]): track for track in thc_tracks
    }

    title_to_id = {
        track["TrackMetadata"]["title"]: track["TrackMetadata"]["TrackId"]
        for track in src_tracks
    }

    unique_normalized_src_titles = set(
        [normalize_text(track["TrackMetadata"]["title"]) for track in src_tracks]
    )

    if len(src_tracks) == 0 or len(thc_tracks) == 0:
        return None

    album_key = thc_tracks[0].album_id

    mapped_entry = calc_optimal_name_mapping(
        [track["TrackMetadata"]["title"] for track in src_tracks], thc_tracks
    )

    sum_score = sum([entry["score"] for entry in mapped_entry.values()])
    total_potential = len(unique_normalized_src_titles) * 100

    debug_info = {
        "album_key": album_key,
        "src_track_len": len(src_tracks),
        "thc_track_len": len(thc_tracks),
        "total_potential": total_potential,
        "actual_score": sum_score.item(),
        "score_ratio": (sum_score / total_potential).item(),
    }

    utils.append_file(
        score_debug_output_path, json.dumps(debug_info, ensure_ascii=False) + "\n"
    )

    if sum_score < total_potential * 0.8:
        return None

    result = {  # track_id: thc_track
        title_to_id[track_title]: entry["matched_with"]
        for track_title, entry in mapped_entry.items()
    }

    return result


def calc_optimal_name_mapping(
    src_track_titles: list, thw_tracks: list
) -> Dict[str, dict]:
    # Maximize the sum of the scores of the thwiki track titles and the src track titles
    # Hint: This is a variant of the assignment problem
    # https://en.wikipedia.org/wiki/Assignment_problem
    src_normalized_list = [(track, normalize_text(track)) for track in src_track_titles]

    thw_normalized_list = [
        (track, normalize_text(json.loads(track.title_jp)[0])) for track in thw_tracks
    ]

    # Create a cost matrix using negative scores because the Hungarian algorithm minimizes cost
    cost_matrix = -np.array(
        [
            [
                fuzz.ratio(src_normalized, thw_normalized)
                for _, thw_normalized in thw_normalized_list
            ]
            for _, src_normalized in src_normalized_list
        ]
    )

    # Solve the assignment problem
    row_ind, col_ind = linear_sum_assignment(cost_matrix)

    # Retrieve the matches
    best_matches = {}
    for src_idx, thw_idx in zip(row_ind, col_ind):
        src_track, _ = src_normalized_list[src_idx]
        thw_track, _ = thw_normalized_list[thw_idx]
        score = -cost_matrix[src_idx, thw_idx]  # Convert back to positive score
        best_matches[src_track] = {"matched_with": thw_track, "score": score}

    return best_matches


def generate_track_formatted(mapped_tracks, abbriv_map: Dict[str, str]):
    track: Track

    fmt = {}
    for remote_id, track in mapped_tracks.items():

        track_fmt_map = {}

        # original
        if not track.original:
            track_fmt_map["original"] = None
        else:
            track_fmt_map["original"] = json.loads(
                resolve_original_tracks(track, abbriv_map)
            )

        track_fmt_map["vocal"] = json.loads(track.vocal) if track.vocal else None
        track_fmt_map["arrangement"] = (
            json.loads(track.arrangement) if track.arrangement else None
        )
        track_fmt_map["lyricist"] = (
            json.loads(track.lyrics_author) if track.lyrics_author else None
        )

        fmt[remote_id] = track_fmt_map

    return fmt


def generate_album_formatted(album: Album, remote_id: str):
    fmt = {}
    fmt["catalog"] = album.catalogno
    fmt["website"] = album.website
    fmt["data_source"] = album.data_source
    fmt["genre"] = json.loads(album.genre) if album.genre else None
    fmt["cover_char"] = json.loads(album.cover_char) if album.cover_char else None
    return fmt


def main():
    with open(merged_output_path, "r", encoding="utf-8") as f:
        id_assignment = json.load(f)

    abbriv_map = load_original_song_map()

    matched_total = 0
    no_match_total = 0
    coll_trk_fmt = {}
    coll_alb_fmt = {}
    for id, entry in id_assignment.items():
        album_id = id

        thc_album = Album.get_or_none(Album.album_id == album_id)
        if thc_album is None:
            print("Album {} not found".format(album_id), end="\r")
            continue

        src_tracks = collect_tracks(entry)
        thc_tracks = list(Track.select().where(Track.album == thc_album))

        mapped_tracks = match_src_thw_tracks_fuzzy(src_tracks, thc_tracks)
        if mapped_tracks is None:
            no_match_total += 1
            print(
                "[{}/{} | {}] Album {} no match".format(
                    matched_total, len(id_assignment), no_match_total, album_id
                ),
                end="\r",
            )
            continue
        else:
            print(
                "[{}/{} | {}] Album {} matched".format(
                    matched_total, len(id_assignment), no_match_total, album_id
                ),
                end="\r",
            )
            matched_total += 1
            trk_fmt = generate_track_formatted(mapped_tracks, abbriv_map)
            alb_fmt = generate_album_formatted(thc_album, album_id)
            coll_trk_fmt.update(trk_fmt)
            coll_alb_fmt[album_id] = alb_fmt

    with open(track_formatted_output_path, "w", encoding="utf-8") as f:
        json.dump(coll_trk_fmt, f, indent=4, ensure_ascii=False)

    print()
    print("Matched ", len(coll_trk_fmt), " tracks")

    with open(album_formatted_output_path, "w", encoding="utf-8") as f:
        json.dump(coll_alb_fmt, f, indent=4, ensure_ascii=False)


if __name__ == "__main__":
    main()