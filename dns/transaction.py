# Copyright (C) Dnspython Contributors, see LICENSE for text of ISC license

import collections

import dns.exception
import dns.name
import dns.rdataclass
import dns.rdataset
import dns.rdatatype
import dns.rrset
import dns.ttl


class TransactionManager:
    def reader(self):
        """Begin a read-only transaction."""
        raise NotImplementedError  # pragma: no cover

    def writer(self, replacement=False):
        """Begin a writable transaction.

        *replacement*, a `bool`.  If `True`, the content of the
        transaction completely replaces any prior content.  If False,
        the default, then the content of the transaction updates the
        existing content.
        """
        raise NotImplementedError  # pragma: no cover


class DeleteNotExact(dns.exception.DNSException):
    """Existing data did not match data specified by an exact delete."""


class ReadOnly(dns.exception.DNSException):
    """Tried to write to a read-only transaction."""


class Transaction:

    def __init__(self, replacement=False, read_only=False):
        self.replacement = replacement
        self.read_only = read_only

    #
    # This is the high level API
    #

    def get(self, name, rdclass, rdtype, covers=dns.rdatatype.NONE):
        """Return the rdataset associated with *name*, *rdclass*, *rdtype*,
        and *covers*, or `None` if not found.

        Note that the returned rdataset is immutable.
        """
        if isinstance(name, str):
            name = dns.name.from_text(name, None)
        rdclass = dns.rdataclass.RdataClass.make(rdclass)
        rdtype = dns.rdatatype.RdataType.make(rdtype)
        rdataset = self._get_rdataset(name, rdclass, rdtype, covers)
        if rdataset is not None and \
           not isinstance(rdataset, dns.rdataset.ImmutableRdataset):
            rdataset = dns.rdataset.ImmutableRdataset(rdataset)
        return rdataset

    def _check_read_only(self):
        if self.read_only:
            raise ReadOnly

    def add(self, *args):
        """Add records.

        The arguments may be:

            - rrset

            - name, rdataset...

            - name, ttl, rdata...
        """
        self._check_read_only()
        return self._add(False, args)

    def replace(self, *args):
        """Replace the existing rdataset at the name with the specified
        rdataset, or add the specified rdataset if there was no existing
        rdataset.

        The arguments may be:

            - rrset

            - name, rdataset...

            - name, ttl, rdata...

        Note that if you want to replace the entire node, you should do
        a delete of the name followed by one or more calls to add() or
        replace().
        """
        self._check_read_only()
        return self._add(True, args)

    def delete(self, *args):
        """Delete records.

        It is not an error if some of the records are not in the existing
        set.

        The arguments may be:

            - rrset

            - name

            - name, rdataclass, rdatatype, [covers]

            - name, rdataset...

            - name, rdata...
        """
        self._check_read_only()
        return self._delete(False, args)

    def delete_exact(self, *args):
        """Delete records.

        The arguments may be:

            - rrset

            - name

            - name, rdataclass, rdatatype, [covers]

            - name, rdataset...

            - name, rdata...

        Raises dns.transaction.DeleteNotExact if some of the records
        are not in the existing set.

        """
        self._check_read_only()
        return self._delete(True, args)

    def name_exists(self, name):
        """Does the specified name exist?"""
        if isinstance(name, str):
            name = dns.name.from_text(name, None)
        return self._name_exists(name)

    def set_serial(self, increment=1, value=None, name=dns.name.empty,
                   rdclass=dns.rdataclass.IN):
        if isinstance(name, str):
            name = dns.name.from_text(name, None)
        rdataset = self._get_rdataset(name, rdclass, dns.rdatatype.SOA,
                                      dns.rdatatype.NONE)
        if rdataset is None or len(rdataset) == 0:
            raise KeyError
        if value is not None:
            serial = value
        else:
            serial = rdataset[0].serial
        serial += increment
        if serial > 0xffffffff or serial < 1:
            serial = 1
        rdata = rdataset[0].replace(serial=serial)
        new_rdataset = dns.rdataset.from_rdata(rdataset.ttl, rdata)
        self.replace(name, new_rdataset)

    def __iter__(self):
        return self._iterate_rdatasets()

    #
    # Helper methods
    #

    def _raise_if_not_empty(self, method, args):
        if len(args) != 0:
            raise TypeError(f'extra parameters to {method}')

    def _rdataset_from_args(self, method, deleting, args):
        try:
            arg = args.popleft()
            if isinstance(arg, dns.rdataset.Rdataset):
                rdataset = arg
            else:
                if deleting:
                    ttl = 0
                else:
                    if isinstance(arg, int):
                        ttl = arg
                        if ttl > dns.ttl.MAX_TTL:
                            raise ValueError(f'{method}: TTL value too big')
                    else:
                        raise TypeError(f'{method}: expected a TTL')
                    arg = args.popleft()
                if isinstance(arg, dns.rdata.Rdata):
                    rdataset = dns.rdataset.from_rdata(ttl, arg)
                else:
                    raise TypeError(f'{method}: expected an Rdata')
            return rdataset
        except IndexError:
            if deleting:
                return None
            else:
                # reraise
                raise TypeError(f'{method}: expected more arguments')

    def _add(self, replace, args):
        try:
            args = collections.deque(args)
            if replace:
                method = 'replace()'
            else:
                method = 'add()'
            arg = args.popleft()
            if isinstance(arg, str):
                arg = dns.name.from_text(arg, None)
            if isinstance(arg, dns.name.Name):
                name = arg
                rdataset = self._rdataset_from_args(method, False, args)
            elif isinstance(arg, dns.rrset.RRset):
                rrset = arg
                name = rrset.name
                # rrsets are also rdatasets, but they don't print the
                # same, so convert.
                rdataset = dns.rdataset.Rdataset(rrset.rdclass, rrset.rdtype,
                                                 rrset.covers, rrset.ttl)
                rdataset.union_update(rrset)
            else:
                raise TypeError(f'{method} requires a name or RRset ' +
                                'as the first argument')
            self._raise_if_not_empty(method, args)
            if not replace:
                existing = self._get_rdataset(name, rdataset.rdclass,
                                              rdataset.rdtype, rdataset.covers)
                if existing is not None:
                    if isinstance(existing, dns.rdataset.ImmutableRdataset):
                        trds = dns.rdataset.Rdataset(existing.rdclass,
                                                     existing.rdtype,
                                                     existing.covers)
                        trds.update(existing)
                        existing = trds
                    rdataset = existing.union(rdataset)
            self._put_rdataset(name, rdataset)
        except IndexError:
            raise TypeError(f'not enough parameters to {method}')

    def _delete(self, exact, args):
        try:
            args = collections.deque(args)
            if exact:
                method = 'delete_exact()'
            else:
                method = 'delete()'
            arg = args.popleft()
            if isinstance(arg, str):
                arg = dns.name.from_text(arg, None)
            if isinstance(arg, dns.name.Name):
                name = arg
                if len(args) > 0 and isinstance(args[0], int):
                    # deleting by type and class
                    rdclass = dns.rdataclass.RdataClass.make(args.popleft())
                    rdtype = dns.rdatatype.RdataType.make(args.popleft())
                    if len(args) > 0:
                        covers = dns.rdatatype.RdataType.make(args.popleft())
                    else:
                        covers = dns.rdatatype.NONE
                    self._raise_if_not_empty(method, args)
                    existing = self._get_rdataset(name, rdclass, rdtype, covers)
                    if existing is None:
                        if exact:
                            raise DeleteNotExact(f'{method}: missing rdataset')
                    else:
                        self._delete_rdataset(name, rdclass, rdtype, covers)
                    return
                else:
                    rdataset = self._rdataset_from_args(method, True, args)
            elif isinstance(arg, dns.rrset.RRset):
                rdataset = arg  # rrsets are also rdatasets
                name = rdataset.name
            else:
                raise TypeError(f'{method} requires a name or RRset ' +
                                'as the first argument')
            self._raise_if_not_empty(method, args)
            if rdataset:
                existing = self._get_rdataset(name, rdataset.rdclass,
                                              rdataset.rdtype, rdataset.covers)
                if existing is not None:
                    if exact:
                        intersection = existing.intersection(rdataset)
                        if intersection != rdataset:
                            raise DeleteNotExact(f'{method}: missing rdatas')
                    rdataset = existing.difference(rdataset)
                    if len(rdataset) == 0:
                        self._delete_rdataset(name, rdataset.rdclass,
                                              rdataset.rdtype, rdataset.covers)
                    else:
                        self._put_rdataset(name, rdataset)
                elif exact:
                    raise DeleteNotExact(f'{method}: missing rdataset')
            else:
                if exact and not self._name_exists(name):
                    raise DeleteNotExact(f'{method}: name not known')
                self._delete_name(name)
        except IndexError:
            raise TypeError(f'not enough parameters to {method}')

    #
    # Transactions are context managers.
    #

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._end_transaction(True)
        else:
            self._end_transaction(False)
        return False

    #
    # This is the low level API, which must be implemented by subclasses
    # of Transaction.
    #

    def _get_rdataset(self, name, rdclass, rdtype, covers):
        """Return the rdataset associated with *name*, *rdclass*, *rdtype*,
        and *covers*, or `None` if not found."""
        raise NotImplementedError  # pragma: no cover

    def _put_rdataset(self, name, rdataset):
        """Store the rdataset."""
        raise NotImplementedError  # pragma: no cover

    def _delete_name(self, name):
        """Delete all data associated with *name*.

        It is not an error if the rdataset does not exist.
        """
        raise NotImplementedError  # pragma: no cover

    def _delete_rdataset(self, name, rdclass, rdtype, covers):
        """Delete all data associated with *name*, *rdclass*, *rdtype*, and
        *covers*.

        It is not an error if the rdataset does not exist.
        """
        raise NotImplementedError  # pragma: no cover

    def _name_exists(self, name):
        """Does name exist?

        Returns a bool.
        """
        raise NotImplementedError  # pragma: no cover

    def _end_transaction(self, commit):
        """End the transaction.

        *commit*, a bool.  If ``True``, commit the transaction, otherwise
        roll it back.

        Raises an exception if committing failed.
        """
        raise NotImplementedError  # pragma: no cover

    def _set_origin(self, origin):
        """Set the origin.

        This method is called when reading a possibly relativized
        source, and an origin setting operation occurs (e.g. $ORIGIN
        in a masterfile).
        """
        raise NotImplementedError  # pragma: no cover

    def _iterate_rdatasets(self):
        """Return an iterator that yields (name, rdataset) tuples.

        Not all Transaction subclasses implement this.
        """
        raise NotImplementedError  # pragma: no cover
