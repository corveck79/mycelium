"""Minimal LZString decoder (decompressFromEncodedURIComponent only).

Port of the pako/lz-string algorithm (https://github.com/pieroxy/lz-string)
used by debridmediamanager.com to pack each shared hashlist into the page's
iframe URL fragment. Only the decompress path is implemented; Mycelium never
needs to compress anything with it.
"""

_KEY_STR_URI_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
_REVERSE_DICT = {c: i for i, c in enumerate(_KEY_STR_URI_SAFE)}


class _Data:
    __slots__ = ("val", "position", "index")

    def __init__(self, val, position, index):
        self.val = val
        self.position = position
        self.index = index


def _read_bits(num_bits, data, reset_value, get_next_value):
    bits = 0
    power = 1
    maxpower = 1 << num_bits
    while power != maxpower:
        resb = data.val & data.position
        data.position >>= 1
        if data.position == 0:
            data.position = reset_value
            data.val = get_next_value(data.index)
            data.index += 1
        bits |= power if resb > 0 else 0
        power <<= 1
    return bits


def _decompress(length, reset_value, get_next_value):
    dictionary = {0: "", 1: "", 2: ""}
    enlarge_in = 4
    dict_size = 4
    num_bits = 3

    data = _Data(val=get_next_value(0), position=reset_value, index=1)

    next_val = _read_bits(2, data, reset_value, get_next_value)
    if next_val == 0:
        c = chr(_read_bits(8, data, reset_value, get_next_value))
    elif next_val == 1:
        c = chr(_read_bits(16, data, reset_value, get_next_value))
    else:
        return ""

    dictionary[3] = c
    w = c
    result = [c]
    while True:
        if data.index > length:
            return ""

        c_code = _read_bits(num_bits, data, reset_value, get_next_value)
        if c_code == 0:
            dictionary[dict_size] = chr(_read_bits(8, data, reset_value, get_next_value))
            dict_size += 1
            c_code = dict_size - 1
            enlarge_in -= 1
        elif c_code == 1:
            dictionary[dict_size] = chr(_read_bits(16, data, reset_value, get_next_value))
            dict_size += 1
            c_code = dict_size - 1
            enlarge_in -= 1
        elif c_code == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

        if c_code in dictionary:
            entry = dictionary[c_code]
        elif c_code == dict_size:
            entry = w + w[0]
        else:
            return None
        result.append(entry)

        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        enlarge_in -= 1

        w = entry
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1


def decompress_from_encoded_uri_component(compressed: str | None) -> str | None:
    if compressed is None:
        return ""
    if compressed == "":
        return None
    compressed = compressed.replace(" ", "+")
    return _decompress(
        len(compressed),
        32,
        lambda index: _REVERSE_DICT[compressed[index]],
    )
