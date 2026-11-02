"""python -m backend.server 的等价入口：python -m backend"""
if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ['--self-check'] and len(sys.argv)<=3:
        from .self_check import main
        raise SystemExit(main(sys.argv[2] if len(sys.argv)==3 else None))
    from .server import main
    main()
