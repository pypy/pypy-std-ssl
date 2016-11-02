import warnings
from _openssl import ffi
from _openssl import lib
from openssl._stdssl.utility import _string_from_asn1
from openssl._stdssl.error import ssl_error, _ssl_seterror

X509_NAME_MAXLEN = 256

def _create_tuple_for_attribute(name, value):
    buf = ffi.new("char[]", X509_NAME_MAXLEN)
    length = lib.OBJ_obj2txt(buf, X509_NAME_MAXLEN, name, 0)
    if length < 0:
        raise _ssl_seterror(None, 0)
    name = ffi.string(buf, length).decode('utf-8')

    buf_ptr = ffi.new("unsigned char**")
    length = lib.ASN1_STRING_to_UTF8(buf_ptr, value)
    if length < 0:
        raise _ssl_seterror(None, 0)
    try:
        value = ffi.string(buf_ptr[0]).decode('utf-8')
    finally:
        lib.OPENSSL_free(buf_ptr[0])
    return (name, value)

def _get_aia_uri(certificate, nid):
    info = lib._X509_get_ext_d2i(certificate, lib.NID_info_access, ffi.NULL, ffi.NULL)
    if (info == ffi.NULL):
        return None;
    if lib.sk_ACCESS_DESCRIPTION_num(info) == 0:
        lib.AUTHORITY_INFO_ACCESS_free(info)
        return None

    lst = []
    count = lib.sk_ACCESS_DESCRIPTION_num(info)
    for i in range(count):
        ad = lib.sk_ACCESS_DESCRIPTION_value(info, i)

        if lib.OBJ_obj2nid(ad.method) != nid or \
           ad.location.type != lib.GEN_URI:
            continue
        uri = ad.location.d.uniformResourceIdentifier
        ostr = ffi.string(uri.data, uri.length).decode('utf-8')
        lst.append(ostr)
    lib.AUTHORITY_INFO_ACCESS_free(info)

    # convert to tuple or None
    if len(lst) == 0: return None
    return tuple(lst)

def _get_peer_alt_names(certificate):
    # this code follows the procedure outlined in
    # OpenSSL's crypto/x509v3/v3_prn.c:X509v3_EXT_print()
    # function to extract the STACK_OF(GENERAL_NAME),
    # then iterates through the stack to add the
    # names.
    peer_alt_names = []

    if certificate == ffi.NULL:
        return None

    # get a memory buffer
    biobuf = lib.BIO_new(lib.BIO_s_mem());

    i = -1
    while True:
        i = lib.X509_get_ext_by_NID(certificate, lib.NID_subject_alt_name, i)
        if i < 0:
            break


        # now decode the altName
        ext = lib.X509_get_ext(certificate, i);
        method = lib.X509V3_EXT_get(ext)
        if method is ffi.NULL:
            raise ssl_error("No method for internalizing subjectAltName!")

        ext_data = lib.X509_EXTENSION_get_data(ext)
        ext_data_len = ext_data.length
        ext_data_value = ffi.new("unsigned char**", ffi.NULL)
        ext_data_value[0] = ext_data.data

        if method.it != ffi.NULL:
            names = lib.ASN1_item_d2i(ffi.NULL, ext_data_value, ext_data_len, lib.ASN1_ITEM_ptr(method.it))
        else:
            names = method.d2i(ffi.NULL, ext_data_value, ext_data_len)

        names = ffi.cast("GENERAL_NAMES*", names)
        count = lib.sk_GENERAL_NAME_num(names)
        for j in range(count):
            # get a rendering of each name in the set of names
            name = lib.sk_GENERAL_NAME_value(names, j);
            _type = name.type
            if _type == lib.GEN_DIRNAME:
                # we special-case DirName as a tuple of
                # tuples of attributes
                v = _create_tuple_for_X509_NAME(name.d.dirn)
                peer_alt_names.append(("DirName", v))
            # GENERAL_NAME_print() doesn't handle NULL bytes in ASN1_string
            # correctly, CVE-2013-4238
            elif _type == lib.GEN_EMAIL:
                v = _string_from_asn1(name.d.rfc822Name)
                peer_alt_names.append(("email", v))
            elif _type == lib.GEN_DNS:
                v = _string_from_asn1(name.d.dNSName)
                peer_alt_names.append(("DNS", v))
            elif _type == lib.GEN_URI:
                v = _string_from_asn1(name.d.uniformResourceIdentifier)
                peer_alt_names.append(("URI", v))
            else:
                # for everything else, we use the OpenSSL print form
                if _type not in (lib.GEN_OTHERNAME, lib.GEN_X400, \
                                 lib.GEN_EDIPARTY, lib.GEN_IPADD, lib.GEN_RID):
                    warnings.warn("Unknown general type %d" % _type, RuntimeWarning)
                    continue
                lib.BIO_reset(biobuf);
                lib.GENERAL_NAME_print(biobuf, name);
                v = _bio_get_str(biobuf)
                idx = v.find(":")
                if idx == -1:
                    return None
                peer_alt_names.append((v[:idx], v[idx:]))

        free_func_addr = ffi.addressof(lib, "GENERAL_NAME_free")
        lib.sk_GENERAL_NAME_pop_free(names, free_func_addr);
    lib.BIO_free(biobuf)
    if peer_alt_names is not None:
        return tuple(peer_alt_names)
    return peer_alt_names

def _create_tuple_for_X509_NAME(xname):
    dn = []
    rdn = []
    rdn_level = -1
    entry_count = lib.X509_NAME_entry_count(xname);
    for index_counter in range(entry_count):
        entry = lib.X509_NAME_get_entry(xname, index_counter);

        # check to see if we've gotten to a new RDN
        _set = lib.X509_NAME_ENTRY_set(entry)
        if rdn_level >= 0:
            if rdn_level != _set:
                dn.append(tuple(rdn))
                rdn = []
        rdn_level = _set

        # now add this attribute to the current RDN
        name = lib.X509_NAME_ENTRY_get_object(entry);
        value = lib.X509_NAME_ENTRY_get_data(entry);
        attr = _create_tuple_for_attribute(name, value);
        if attr == ffi.NULL:
            pass # TODO error
            raise NotImplementedError
        rdn.append(attr)

    # now, there's typically a dangling RDN
    if rdn and len(rdn) > 0:
        dn.append(tuple(rdn))

    return tuple(dn)

STATIC_BIO_BUF = ffi.new("char[]", 2048)

def _bio_get_str(biobuf):
    length = lib.BIO_gets(biobuf, STATIC_BIO_BUF, len(STATIC_BIO_BUF)-1)
    if length < 0:
        if biobuf: lib.BIO_free(biobuf)
        raise _ssl_error(None) # TODO _setSSLError
    return ffi.string(STATIC_BIO_BUF, length).decode('utf-8')

def _decode_certificate(certificate):
    retval = {}

    peer = _create_tuple_for_X509_NAME(lib.X509_get_subject_name(certificate));
    if not peer:
        return None
    retval["subject"] = peer

    issuer = _create_tuple_for_X509_NAME(lib.X509_get_issuer_name(certificate));
    if not issuer:
        return None
    retval["issuer"] = issuer

    version = lib.X509_get_version(certificate) + 1
    if version == 0:
        return None
    retval["version"] = version

    try:
        biobuf = lib.BIO_new(lib.BIO_s_mem());

        lib.BIO_reset(biobuf);
        serialNumber = lib.X509_get_serialNumber(certificate);
        # should not exceed 20 octets, 160 bits, so buf is big enough
        lib.i2a_ASN1_INTEGER(biobuf, serialNumber)
        buf = ffi.new("char[]", 2048)
        length = lib.BIO_gets(biobuf, buf, len(buf)-1)
        if length < 0:
            if biobuf: lib.BIO_free(biobuf)
            raise _ssl_error(None) # TODO _setSSLError
        retval["serialNumber"] = ffi.string(buf, length).decode('utf-8')

        lib.BIO_reset(biobuf);
        notBefore = lib.X509_get_notBefore(certificate);
        lib.ASN1_TIME_print(biobuf, notBefore);
        length = lib.BIO_gets(biobuf, buf, len(buf)-1);
        if length < 0:
            if biobuf: lib.BIO_free(biobuf)
            raise _ssl_error(None) # TODO _setSSLError
        retval["notBefore"] = ffi.string(buf, length).decode('utf-8')

        lib.BIO_reset(biobuf);
        notAfter = lib.X509_get_notAfter(certificate);
        lib.ASN1_TIME_print(biobuf, notAfter);
        length = lib.BIO_gets(biobuf, buf, len(buf)-1);
        if length < 0:
            raise _ssl_error(None) # TODO _setSSLError
        retval["notAfter"] = ffi.string(buf, length).decode('utf-8')

        # Now look for subjectAltName

        peer_alt_names = _get_peer_alt_names(certificate);
        if not peer_alt_names:
            if biobuf: lib.BIO_free(biobuf)
            return None
        retval["subjectAltName"] = peer_alt_names

        # Authority Information Access: OCSP URIs
        obj = _get_aia_uri(certificate, lib.NID_ad_OCSP)
        if obj:
            retval["OCSP"] = obj

        obj = _get_aia_uri(certificate, lib.NID_ad_ca_issuers)
        if obj:
            retval["caIssuers"] = obj

        # CDP (CRL distribution points)
        obj = _get_crl_dp(certificate)
        if obj:
            retval["crlDistributionPoints"] = obj
    finally:
        lib.BIO_free(biobuf)

    return retval


def _get_crl_dp(certificate):
#    STACK_OF(DIST_POINT) *dps;
#    int i, j;
#    PyObject *lst, *res = NULL;
#
    if lib.OPENSSL_VERSION_NUMBER < 0x10001000:
        dps = lib.X509_get_ext_d2i(certificate, lib.NID_crl_distribution_points, ffi.NULL, ffi.NULL)
    else:
        # Calls x509v3_cache_extensions and sets up crldp
        lib.X509_check_ca(certificate)
        dps = lib._X509_get_crldp(certificate)
    if dps is ffi.NULL:
        return None

    lst = []
    count = lib.sk_DIST_POINT_num(dps)
    for i in range(count):
        dp = lib.sk_DIST_POINT_value(dps, i);
        gns = dp.distpoint.name.fullname;

        jcount = lib.sk_GENERAL_NAME_num(gns)
        for j in range(jcount):
            gn = lib.sk_GENERAL_NAME_value(gns, j)
            if gn.type != lib.GEN_URI:
                continue

            uri = gn.d.uniformResourceIdentifier;
            ouri = ffi.string(ffi.cast("char*", uri.data), uri.length).decode('utf-8')
            lst.append(ouri)

    if lib.OPENSSL_VERSION_NUMBER < 0x10001000:
        lib.sk_DIST_POINT_free(dps);

    if len(lst) == 0: return None
    return tuple(lst)

def _test_decode_cert(path):
    cert = lib.BIO_new(lib.BIO_s_file())
    if cert is ffi.NULL:
        lib.BIO_free(cert)
        raise ssl_error("Can't malloc memory to read file")

    # REVIEW how to encode this properly?
    epath = path.encode()
    if lib.BIO_read_filename(cert, epath) <= 0:
        lib.BIO_free(cert)
        raise ssl_error("Can't open file")

    x = lib.PEM_read_bio_X509_AUX(cert, ffi.NULL, ffi.NULL, ffi.NULL)
    if x is ffi.NULL:
        ssl_error("Error decoding PEM-encoded file")

    retval = _decode_certificate(x)
    lib.X509_free(x);

    if cert != ffi.NULL:
        lib.BIO_free(cert)
    return retval
