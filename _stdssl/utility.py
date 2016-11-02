from _openssl import ffi
from _openssl import lib

def _string_from_asn1(asn1):
    data = lib.ASN1_STRING_data(asn1)
    length = lib.ASN1_STRING_length(asn1)
    return ffi.string(ffi.cast("char*",data), length).decode('utf-8')
