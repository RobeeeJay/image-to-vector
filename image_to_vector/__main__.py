if __name__ == "__main__":
    # Imported lazily: spawned trace workers re-import this module as
    # __mp_main__, and they have no use for Qt widgets.
    from image_to_vector.app import main

    raise SystemExit(main())
